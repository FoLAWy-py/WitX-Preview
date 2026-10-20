"""Device pool, stage 1: what is connected, and what each device still needs."""

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest
from witx_gateway import discovery
from witx_gateway.devices.android import AndroidDevice
from witx_gateway.devices.base import DeviceError
from witx_gateway.devices.ios import IOSDevice
from witx_gateway.discovery import (
    KEEP_WARM_BACKOFF,
    Found,
    Inventory,
    Scan,
    parse_adb,
    parse_devicectl,
    parse_simctl,
    report,
)
from witx_gateway.main import build_device, configure_device

ADB = """List of devices attached
TESTANDROID0001       device usb:17956864X product:TDT-MA01 model:TDT_MA01 device:TDTMA-M
R58N12345              unauthorized usb:1-1 transport_id:9
"""


def iphone(name: str, udid: str, **connection: str) -> dict[str, object]:
    return {
        "hardwareProperties": {"platform": "iOS", "reality": "physical", "udid": udid},
        "connectionProperties": {"pairingState": "paired", **connection},
        "deviceProperties": {"name": name, "developerModeStatus": "enabled"},
    }


def test_android_phones_are_found_with_their_usb_debugging_state() -> None:
    assert parse_adb(ADB) == [
        Found("android", "TESTANDROID0001", "TDT MA01"),
        Found("android", "R58N12345", "R58N12345", ("adb_unauthorized",)),
    ]


def test_only_iphones_connected_now_are_found_not_ones_merely_nearby() -> None:
    mine = iphone("Alex's iPhone", "00000000-0000", transportType="wired")
    nearby = iphone("A neighbour's iPhone", "00008110-0006", transportType="localNetwork")
    nearby["connectionProperties"]["tunnelState"] = "disconnected"  # type: ignore[index]
    watch = {"hardwareProperties": {"platform": "watchOS", "udid": "W"}}
    off = iphone("Test iPhone", "00000000-0003", transportType="wired")
    off["deviceProperties"]["developerModeStatus"] = "disabled"  # type: ignore[index]
    found = parse_devicectl({"result": {"devices": [mine, nearby, watch, off]}})
    assert found == [
        Found("ios", "00000000-0000", "Alex's iPhone"),
        Found("ios", "00000000-0003", "Test iPhone", ("developer_mode_off",)),
    ]


def test_running_simulators_are_found() -> None:
    data = {
        "devices": {
            "iOS-27": [{"udid": "A", "state": "Booted"}, {"udid": "B", "state": "Shutdown"}]
        }
    }
    assert parse_simctl(data) == [Found("ios", "A", "A")]


def android(serial: str) -> AndroidDevice:
    return AndroidDevice(serial=serial, name="TDT", appium_url="http://127.0.0.1:4723")


def test_a_configured_device_reports_what_it_still_needs() -> None:
    tdt = android("TESTANDROID0001")
    unplugged = android("emulator-5554")
    iphone_unsigned = IOSDevice(
        udid="00000000-0000", name="iPhone", appium_url="http://127.0.0.1:4723"
    )
    scan = Scan(
        android=[Found("android", "TESTANDROID0001", "TDT MA01", ("adb_unauthorized",))],
        iphones=[Found("ios", "00000000-0000", "iPhone")],
        simulators=[],
        appium_up={"http://127.0.0.1:4723": True},
        drivers={"uiautomator2"},
    )
    problems = {d.native_id: d.problems for d in report([tdt, unplugged, iphone_unsigned], scan)}
    assert problems == {
        "TESTANDROID0001": ["adb_unauthorized"],
        "emulator-5554": ["disconnected"],
        "00000000-0000": ["driver_missing", "wda_unsigned"],  # no xcuitest, no signing
    }


async def test_a_device_plugged_in_later_gets_its_own_adapter_and_ports() -> None:
    scans = [
        Scan(android=[], iphones=[], simulators=[]),
        Scan(android=[Found("android", "NEW1", "Pixel 9")], iphones=[], simulators=[]),
    ]

    async def scan() -> Any:
        return scans.pop(0)

    settings = [{"platform": "android", "native_id": "EMU", "name": "Emulator"}]
    inventory = Inventory(
        settings, build=build_device, appium_url="http://127.0.0.1:4723", scan=scan
    )
    first = await inventory.refresh()
    assert [(i.native_id, i.problems) for i in first] == [("EMU", ["disconnected"])]
    second = await inventory.refresh()
    assert [(i.native_id, i.name) for i in second] == [("EMU", "Emulator"), ("NEW1", "Pixel 9")]
    new = inventory.devices[1]
    assert new._ports == {  # never the emulator's 8100
        "appium:systemPort": 8101,
        "appium:mjpegServerPort": 9101,
    }


def test_a_running_simulator_is_ready_and_a_shut_down_one_is_not() -> None:
    booted = IOSDevice(
        udid="11111111-1111-4111-8111-111111111111",
        name="iPhone 18 Pro",
        appium_url="http://127.0.0.1:4723",
    )
    off = IOSDevice(
        udid="22222222-2222-4222-8222-222222222222",
        name="iPhone 18 Pro",
        appium_url="http://127.0.0.1:4723",
    )
    scan = Scan(
        android=[], iphones=[], simulators=[Found("ios", booted.native_id, "iPhone 18 Pro")]
    )
    assert [i.problems for i in report([booted, off], scan)] == [[], ["disconnected"]]


def test_without_the_platform_tools_the_gateway_says_so() -> None:
    scan = Scan(
        android=None, iphones=None, simulators=None, appium_up={"http://127.0.0.1:4723": False}
    )
    (info,) = report([android("TESTANDROID0001")], scan)
    assert info.problems == ["tool_missing", "appium_unreachable"]


class Warmed(AndroidDevice):
    """An adapter that counts the times its session is kept ready."""

    calls = 0
    fail = False
    hold: asyncio.Event | None = None
    reason = "timeout"

    async def warm(self) -> None:
        type(self).calls += 1
        if self.hold is not None:
            await self.hold.wait()
        if type(self).fail:
            self.locked = type(self).reason == "locked"  # as the real check does
            raise DeviceError(type(self).reason)
        self.locked = False


def warm_inventory(
    scans: list[Scan], *, keep_warm: bool = True, warm_devices: bool = True
) -> tuple[Inventory, Any]:
    Warmed.calls, Warmed.fail, Warmed.hold = 0, False, None

    async def scan() -> Any:
        return scans.pop(0) if len(scans) > 1 else scans[0]

    def build(spec: dict[str, Any], ports: Any) -> Warmed:
        return Warmed(
            serial=spec["native_id"],
            name=spec.get("name", "x"),
            appium_url="http://127.0.0.1:4723",
            keep_warm=bool(spec.get("keep_warm", False)),
        )

    inventory = Inventory(
        [],
        build=build,
        appium_url="http://127.0.0.1:4723",
        scan=scan,
        keep_warm=keep_warm,
        warm_devices=warm_devices,
    )
    return inventory, scan


READY_PHONE = Scan(
    android=[Found("android", "P1", "Pixel")],
    iphones=[],
    simulators=[],
    appium_up={"http://127.0.0.1:4723": True},
    drivers={"uiautomator2"},
)


async def test_a_running_gateway_keeps_the_agent_of_a_ready_phone_running() -> None:
    inventory, _ = warm_inventory([READY_PHONE])
    await inventory.refresh()  # found now: its adapter is built and warmed
    await asyncio.sleep(0)
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == 2  # once per scan, while the last one has finished


async def test_a_phone_is_not_warmed_twice_at_once() -> None:
    inventory, _ = warm_inventory([READY_PHONE])
    Warmed.hold = None
    await inventory.refresh()
    device = inventory.devices[0]
    assert isinstance(device, Warmed)
    device.hold = asyncio.Event()  # opening the agent takes a while
    await asyncio.sleep(0)
    await inventory.refresh()
    await asyncio.sleep(0)
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == 1  # still opening: the later scans do not start a second one
    device.hold.set()
    await asyncio.sleep(0)
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == 2  # done: the next scan checks it again


async def test_doctor_and_setup_only_look_they_never_start_an_agent() -> None:
    inventory, _ = warm_inventory([READY_PHONE], warm_devices=False)
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == 0


async def test_a_phone_that_is_not_marked_keep_warm_is_left_alone() -> None:
    inventory, _ = warm_inventory([READY_PHONE], keep_warm=False)
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == 0


async def test_a_phone_with_something_left_to_fix_is_not_warmed() -> None:
    unauthorized = Scan(
        android=[Found("android", "P1", "Pixel", ("adb_unauthorized",))],
        iphones=[],
        simulators=[],
        appium_up={"http://127.0.0.1:4723": True},
        drivers={"uiautomator2"},
    )
    inventory, _ = warm_inventory([unauthorized])
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == 0


async def test_a_phone_whose_agent_could_not_be_kept_is_left_alone_for_a_while() -> None:
    inventory, _ = warm_inventory([READY_PHONE])
    await inventory.refresh()
    await asyncio.sleep(0)
    Warmed.fail = True
    await inventory.refresh()
    await asyncio.sleep(0)
    calls = Warmed.calls
    for _ in range(3):
        await inventory.refresh()
        await asyncio.sleep(0)
    assert Warmed.calls == calls  # backing off: a hung agent takes every command into its queue


async def test_a_session_the_driver_lost_is_opened_again_by_the_next_scan() -> None:
    inventory, _ = warm_inventory([READY_PHONE])
    await inventory.refresh()
    await asyncio.sleep(0)
    Warmed.fail, Warmed.reason = True, "session_lost"
    await inventory.refresh()
    await asyncio.sleep(0)
    calls = Warmed.calls
    Warmed.fail = False
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == calls + 1  # no waiting: the agent was plainly gone
    Warmed.reason = "timeout"


async def test_a_phone_whose_agent_could_not_be_kept_is_reported_as_not_responding() -> None:
    inventory, _ = warm_inventory([READY_PHONE])
    Warmed.fail = True
    (info,) = await inventory.refresh()
    assert info.problems == []  # not tried yet
    await asyncio.sleep(0)
    assert inventory.changed.is_set()  # reported now, not at the next scan
    (info,) = await inventory.refresh()
    assert info.problems == ["not_responding"]


async def test_a_phone_not_responding_is_tried_again_after_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock[0])
    inventory, _ = warm_inventory([READY_PHONE])
    Warmed.fail = True
    await inventory.refresh()
    await asyncio.sleep(0)
    await inventory.refresh()  # within the backoff
    await asyncio.sleep(0)
    assert Warmed.calls == 1
    clock[0] += KEEP_WARM_BACKOFF
    await inventory.refresh()
    await asyncio.sleep(0)
    assert Warmed.calls == 2  # tried again, though it reports a problem of its own


async def test_a_phone_not_responding_is_ready_again_once_its_agent_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock[0])
    inventory, _ = warm_inventory([READY_PHONE])
    Warmed.fail = True
    await inventory.refresh()
    await asyncio.sleep(0)
    inventory.changed.clear()
    Warmed.fail = False
    clock[0] += KEEP_WARM_BACKOFF
    (info,) = await inventory.refresh()
    assert info.problems == ["not_responding"]
    await asyncio.sleep(0)
    assert inventory.changed.is_set()
    (info,) = await inventory.refresh()
    assert info.problems == []


async def test_a_lost_session_does_not_count_as_not_responding() -> None:
    inventory, _ = warm_inventory([READY_PHONE])
    Warmed.fail, Warmed.reason = True, "session_lost"
    await inventory.refresh()
    await asyncio.sleep(0)
    (info,) = await inventory.refresh()
    assert info.problems == []
    assert not inventory.changed.is_set()
    Warmed.reason = "timeout"


async def test_a_phone_kept_warm_and_found_locked_says_so_and_is_looked_at_again_each_scan() -> (
    None
):
    inventory, _ = warm_inventory([READY_PHONE])
    Warmed.fail, Warmed.reason = True, "locked"
    await inventory.refresh()
    await asyncio.sleep(0)
    assert inventory.changed.is_set()  # reported now, not at the next scan
    (info,) = await inventory.refresh()
    assert info.problems == ["locked"]  # not "not responding": it answers, its screen is off
    await asyncio.sleep(0)
    assert Warmed.calls == 2  # no backoff: looking is cheap
    Warmed.fail = False  # someone unlocked it
    inventory.changed.clear()
    await inventory.refresh()
    await asyncio.sleep(0)
    assert inventory.changed.is_set()
    (info,) = await inventory.refresh()
    assert info.problems == []
    Warmed.reason = "timeout"


class Looked(AndroidDevice):
    """A phone not kept warm, found locked when it was used; it is unlocked when
    ``unlocked`` is set."""

    unlocked = False
    looks = 0

    async def check_lock(self) -> None:
        type(self).looks += 1
        self.locked = not type(self).unlocked
        if self.locked:
            raise DeviceError("locked")


async def test_a_phone_found_locked_while_used_is_looked_at_until_someone_unlocks_it() -> None:
    Looked.unlocked, Looked.looks = False, 0
    phone = Looked(serial="P1", name="Pixel", appium_url="http://127.0.0.1:4723")
    phone.locked = True
    phone._session_id = "S1"  # a session is open: it can be looked at
    inventory = Inventory(
        [],
        build=lambda spec, ports: phone,
        appium_url="http://127.0.0.1:4723",
        scan=lambda: _scan(READY_PHONE),
        warm_devices=True,
    )
    (info,) = await inventory.refresh()
    assert info.problems == ["locked"]
    await asyncio.sleep(0)
    assert Looked.looks == 1
    Looked.unlocked = True
    await inventory.refresh()
    await asyncio.sleep(0)
    assert inventory.changed.is_set()
    (info,) = await inventory.refresh()
    assert info.problems == []


async def _scan(scan: Scan) -> Scan:
    return scan


def test_a_phone_with_something_else_to_fix_reports_that_rather_than_not_responding() -> None:
    unplugged = Scan(android=[], iphones=[], simulators=[], drivers={"uiautomator2"})
    (info,) = report([android("P1")], unplugged, {"P1"})
    assert info.problems == ["disconnected"]


# ---------------------------------------------------------------- settings read again

IPHONE_17 = "00000000-0000000000000001"
SIM_18 = "22222222-2222-4222-8222-222222222222"
TWO_IPHONES = Scan(
    android=[],
    iphones=[Found("ios", IPHONE_17, "iPhone 17 Pro")],
    simulators=[Found("ios", SIM_18, "iPhone 18 Pro")],
    appium_up={"http://127.0.0.1:4723": True},
    drivers={"xcuitest"},
)
SIGNING = (
    "\n[[devices]]\n"
    'platform = "ios"\n'
    f'native_id = "{IPHONE_17}"\n'
    'name = "iPhone 17 Pro"\n'
    'capabilities = { "appium:xcodeOrgId" = "TESTTEAM01" }\n'
)


def settings_inventory(config: Path, text: str = "") -> Inventory:
    config.write_text('cloud_url = "http://127.0.0.1:8000"\n' + text)
    settings = tomllib.loads(config.read_text()).get("devices", [])

    async def scan() -> Any:
        return TWO_IPHONES

    return Inventory(
        settings,
        build=lambda spec, ports: build_device(spec, ports),
        configure=configure_device,
        appium_url="http://127.0.0.1:4723",
        scan=scan,
        settings_file=config,
    )


async def test_a_signing_entry_added_while_the_gateway_runs_is_applied_at_the_next_scan(
    tmp_path: Path,
) -> None:
    inventory = settings_inventory(tmp_path / "gateway.toml")
    problems = {i.native_id: i.problems for i in await inventory.refresh()}
    assert problems[IPHONE_17] == ["wda_unsigned"]
    with (tmp_path / "gateway.toml").open("a") as handle:  # what `setup` does
        handle.write(SIGNING)
    problems = {i.native_id: i.problems for i in await inventory.refresh()}
    assert problems[IPHONE_17] == []  # the same adapter, now signed: no restart


async def test_a_changed_entry_leaves_the_other_devices_and_open_sessions_alone(
    tmp_path: Path,
) -> None:
    inventory = settings_inventory(tmp_path / "gateway.toml")
    await inventory.refresh()
    by_id = {d.native_id: d for d in inventory.devices}
    simulator = by_id[SIM_18]
    assert isinstance(simulator, IOSDevice)
    simulator._session_id = "busy-with-a-task"  # type: ignore[attr-defined]
    with (tmp_path / "gateway.toml").open("a") as handle:
        handle.write(SIGNING)
    await inventory.refresh()
    assert {d.native_id: d for d in inventory.devices} == by_id  # no adapter was rebuilt
    assert simulator.session_open and simulator._session_id == "busy-with-a-task"  # type: ignore[attr-defined]


async def test_a_configuration_file_that_no_longer_parses_keeps_the_settings_last_read(
    tmp_path: Path,
) -> None:
    config = tmp_path / "gateway.toml"
    inventory = settings_inventory(config, SIGNING)
    await inventory.refresh()
    config.write_text("this is not toml")  # half-written, say
    problems = {i.native_id: i.problems for i in await inventory.refresh()}
    assert problems[IPHONE_17] == []
