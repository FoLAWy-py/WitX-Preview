"""`witx-gateway setup`: the steps it plans for what is missing, with nothing that could
run what the cloud or a device name says, and what it leaves to a person."""

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from witx_contracts import gateway as wire
from witx_gateway import setup
from witx_gateway.discovery import Scan

IPHONE = "00000000-0000000000000001"
CONFIG = Path("/tmp/witx/gateway.toml")
WDA = Path("/home/u/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent")
READY = Scan(android=[], iphones=[], simulators=[], drivers={"uiautomator2", "xcuitest"})
MAC = setup.Machine(
    macos=True,
    brew=True,
    node=True,
    appium=True,
    emulator="/sdk/emulator/emulator",
    wda_dir=WDA,
    teams=("TESTTEAM01",),
    simulators=(("11111111-1111-4111-8111-111111111111", "iPhone 18 Pro"),),
    avds=("Pixel_9",),
)


def device(problems: list[str], native_id: str = IPHONE, name: str = "Alex's iPhone 17 Pro"):  # type: ignore[no-untyped-def]
    return wire.DeviceInfo(
        native_id=native_id,
        platform="ios",
        name=name,
        capabilities=[],
        problems=problems,  # type: ignore[arg-type]
    )


def planned(**over: object) -> setup.Plan:
    facts: dict[str, object] = {
        "scan": READY,
        "devices": [],
        "machine": MAC,
        "appium_url": "http://127.0.0.1:4723",
        "appium_up": True,
        "config": CONFIG,
        "configured": set(),
        "log_dir": Path("/tmp/witx"),
    }
    return setup.plan(**{**facts, **over})  # type: ignore[arg-type]


def test_a_computer_with_everything_has_nothing_to_set_up() -> None:
    assert planned() == setup.Plan()


def test_missing_tools_are_installed_in_the_order_they_depend_on_each_other() -> None:
    scan = Scan(android=None, iphones=[], simulators=[], drivers=None)
    bare = setup.Machine(
        macos=True, brew=True, node=False, appium=False, emulator=None, wda_dir=None, teams=()
    )
    result = planned(scan=scan, machine=bare, appium_up=False)
    assert [s.argv for s in result.steps] == [
        ("brew", "install", "--cask", "android-platform-tools"),
        ("brew", "install", "node"),
        ("npm", "install", "-g", "appium@3"),
        ("appium", "driver", "install", "uiautomator2"),
        ("appium", "driver", "install", "xcuitest"),
        ("appium", "--address", "127.0.0.1", "--port", "4723", "--log-level", "warn"),
    ]
    assert result.steps[-1].keep_running and result.steps[-1].until.endswith("/status")  # type: ignore[union-attr]


def test_only_the_drivers_that_are_missing_are_installed() -> None:
    scan = Scan(android=[], iphones=[], simulators=[], drivers={"uiautomator2"})
    assert [s.argv for s in planned(scan=scan).steps] == [
        ("appium", "driver", "install", "xcuitest")
    ]


def test_off_a_mac_or_without_brew_what_cannot_be_installed_is_left_to_the_person() -> None:
    scan = Scan(android=None, iphones=None, simulators=None, drivers={"uiautomator2"})
    linux = setup.Machine(
        macos=False, brew=False, node=True, appium=True, emulator=None, wda_dir=None, teams=()
    )
    result = planned(scan=scan, machine=linux)
    assert result.steps == [] and any("platform-tools" in t for t in result.todo)


def test_appium_is_started_knowing_where_the_android_sdk_is() -> None:
    machine = replace(MAC, android_home=Path("/sdk"))
    start = planned(machine=machine, appium_up=False).steps[-1]
    assert start.keep_running and start.env == (("ANDROID_HOME", "/sdk"),)


def test_a_started_process_gets_the_environment_its_step_names(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    show = "import os; print(os.environ['ANDROID_HOME'], flush=True)"
    step = setup.Step(
        "show",
        (sys.executable, "-c", show),
        keep_running=True,
        log=log,
        env=(("ANDROID_HOME", "/sdk"),),
    )
    ok, _ = asyncio.run(setup.run_step(step))
    for _ in range(50):
        if log.exists() and log.read_text():
            break
        time.sleep(0.1)
    assert ok and log.read_text().strip() == "/sdk"


def test_appium_on_another_computer_is_not_started_from_here() -> None:
    result = planned(appium_url="http://10.0.0.5:4723", appium_up=False)
    assert result.steps == [] and any("10.0.0.5" in t for t in result.todo)


def test_a_simulator_or_emulator_is_booted_by_name_or_udid() -> None:
    result = planned(boot=["iphone 18 pro", "Pixel_9", "Nokia"])
    assert [s.argv for s in result.steps] == [
        ("xcrun", "simctl", "boot", "11111111-1111-4111-8111-111111111111"),
        ("/sdk/emulator/emulator", "-avd", "Pixel_9"),
    ]
    assert result.steps[1].keep_running and any("Nokia" in t for t in result.todo)


def test_an_unsigned_iphone_is_built_first_then_signed_with_the_team_of_the_key_store() -> None:
    # Built before the entry is written: a running Gateway reads the entry at once
    # and would otherwise count the phone as signed while WebDriverAgent is not on it yet.
    result = planned(devices=[device(["wda_unsigned"])])
    build, write = result.steps
    path, text = write.append  # type: ignore[misc]
    assert path == CONFIG
    assert f'native_id = "{IPHONE}"' in text and '"appium:xcodeOrgId" = "TESTTEAM01"' in text
    assert '"appium:updatedWDABundleId" = "com.witx.webdriveragentrunner.testteam01"' in text
    assert build.argv[:3] == ("xcodebuild", "build-for-testing", "-project")
    assert f"id={IPHONE}" in build.argv and "DEVELOPMENT_TEAM=TESTTEAM01" in build.argv
    assert "-allowProvisioningUpdates" in build.argv and build.cwd == WDA
    assert any("trust the developer certificate" in t for t in result.todo)


def test_the_config_text_setup_adds_is_valid_toml_with_the_device_s_name() -> None:
    import tomllib

    text = setup.signing_block(
        udid=IPHONE, name='Alex "17" Pro \\ é', team="TESTTEAM01", bundle="a.b.c"
    )
    (entry,) = tomllib.loads(text)["devices"]
    assert entry["native_id"] == IPHONE and entry["name"] == 'Alex "17" Pro \\ é'
    assert entry["capabilities"]["appium:xcodeSigningId"] == "Apple Development"


def test_a_device_with_an_entry_already_is_not_rewritten_only_told_what_to_add() -> None:
    result = planned(devices=[device(["wda_unsigned"])], configured={IPHONE})
    assert [s.append for s in result.steps] == [None]  # only the build
    assert any("already has an entry" in t and "TESTTEAM01" in t for t in result.todo)


def test_several_teams_are_not_chosen_for_the_person() -> None:
    two = setup.Machine(**{**MAC.__dict__, "teams": ("TESTTEAM01", "ABCDE12345")})
    result = planned(devices=[device(["wda_unsigned"])], machine=two)
    assert result.steps == [] and any("--team" in t for t in result.todo)
    chosen = planned(devices=[device(["wda_unsigned"])], machine=two, team="ABCDE12345")
    assert "DEVELOPMENT_TEAM=ABCDE12345" in chosen.steps[0].argv


@pytest.mark.parametrize("team", ["testteam01", "TESTTEAM01; rm -rf /", "SHORT", ""])
def test_a_team_that_is_not_a_team_id_is_refused(team: str) -> None:
    result = planned(devices=[device(["wda_unsigned"])], team=team)
    assert result.steps == [] and any("team ID" in t for t in result.todo)


def test_a_device_id_that_is_not_a_udid_is_never_put_into_a_command() -> None:
    bad = device(["wda_unsigned"], native_id="not-a-udid")
    result = planned(devices=[bad])
    assert result.steps == [] and any("not one setup can use" in t for t in result.todo)


@pytest.mark.parametrize(
    ("machine", "word"),
    [
        (setup.Machine(**{**MAC.__dict__, "teams": ()}), "Apple Development certificate"),
        (setup.Machine(**{**MAC.__dict__, "wda_dir": None}), "xcuitest driver is not installed"),
    ],
)
def test_signing_that_cannot_be_planned_says_why(machine: setup.Machine, word: str) -> None:
    result = planned(devices=[device(["wda_unsigned"])], machine=machine)
    assert result.steps == [] and any(word in t for t in result.todo)


def test_what_a_person_must_do_on_the_phone_is_only_told() -> None:
    result = planned(devices=[device(["not_paired", "developer_mode_off"])])
    assert result.steps == [] and len(result.todo) == 2
    assert "Trust This Computer" in result.todo[0]


def test_the_certificate_team_is_the_ou_not_the_id_in_the_name() -> None:
    subject = (
        "subject=UID=TESTUSER01, CN=Apple Development: a@b.c (TESTCERT01), OU=TESTTEAM01, O=N, C=US"
    )
    assert setup.team_of(subject) == "TESTTEAM01"
    assert setup.team_of("subject=CN=nobody") is None


def test_only_valid_apple_development_identities_are_read_from_the_key_store() -> None:
    text = (
        '  1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "Apple Development: a@b.c (TESTCERT01)"\n'
        '  2) BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB "Developer ID Application: Acme (XYZ)"\n'
        "     2 valid identities found\n"
    )
    assert setup.parse_identities(text) == ["Apple Development: a@b.c (TESTCERT01)"]


def test_the_steps_run_in_order_and_stop_at_the_first_that_fails() -> None:
    steps = [setup.Step("one", ("a",)), setup.Step("two", ("b",)), setup.Step("three", ("c",))]
    ran: list[str] = []

    async def run(step: setup.Step) -> tuple[bool, str]:
        ran.append(step.name)
        return step.name != "two", "no luck"

    said: list[str] = []
    assert not asyncio.run(setup.execute(setup.Plan(steps), run=run, say=said.append))
    assert ran == ["one", "two"] and "  failed: no luck" in said


def test_setup_adds_the_signing_entry_to_the_config_file(tmp_path: Path) -> None:
    config = tmp_path / "gateway.toml"
    config.write_text('cloud_url = "http://x"\n')
    block = setup.signing_block(udid=IPHONE, name="P", team="TESTTEAM01", bundle="a.b.c")
    ok, _ = asyncio.run(setup.run_step(setup.Step("sign", append=(config, block))))
    assert (
        ok
        and config.read_text().startswith('cloud_url = "http://x"\n')
        and IPHONE in config.read_text()
    )
