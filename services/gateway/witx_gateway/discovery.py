"""The devices this Gateway drives, and what each still needs before it can take tasks
(device pool, #33). Read-only: nothing here installs or changes anything.

Discovery uses the platform tools on this machine: ``adb devices`` for Android,
``xcrun devicectl`` for real iPhones and ``xcrun simctl`` for running simulators. Every
device found gets an adapter; whether it may take tasks is decided in the cloud, where
an owner enables it.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import tomllib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from witx_contracts import gateway as wire
from witx_contracts.common import Platform, Problem

from witx_gateway.devices.appium import AppiumDevice
from witx_gateway.devices.base import Device, DeviceError
from witx_gateway.devices.ios import SIMULATOR_UDID, IOSDevice
from witx_gateway.ports import Ports

DRIVERS: dict[str, str] = {"android": "uiautomator2", "ios": "xcuitest"}
DRIVER_RECHECK = 300  # seconds; drivers are installed rarely
# A phone whose agent could not be started or kept is left alone this long (a hung agent
# takes every command that is sent to it into its queue).
KEEP_WARM_BACKOFF = 60.0
# Looking again whether a locked phone was unlocked takes two cheap commands; a phone that
# does not answer them in this long is looked at again at the next scan.
RECHECK_TIMEOUT = 5.0

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Found:
    platform: Platform
    native_id: str
    name: str
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True)
class Scan:
    """One look at this machine. None: the tool to look with is not installed."""

    android: list[Found] | None
    iphones: list[Found] | None
    simulators: list[Found] | None
    appium_up: dict[str, bool] = field(default_factory=dict)  # by Appium URL
    drivers: set[str] | None = None  # installed Appium drivers, None if unknown


def parse_adb(text: str) -> list[Found]:
    """``adb devices -l``."""
    found = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), serial)
        problems: tuple[Problem, ...] = ()
        if state == "unauthorized":
            problems = ("adb_unauthorized",)
        elif state == "offline":
            problems = ("adb_offline",)
        found.append(Found("android", serial, model.replace("_", " "), problems))
    return found


def parse_devicectl(data: dict[str, Any]) -> list[Found]:
    """``xcrun devicectl list devices --json-output``: real iPhones connected now, by USB
    or with a network tunnel up. Devices that are only paired and nearby (another
    person's phone on the same Wi-Fi), watches and simulators are left out."""
    found = []
    for device in data.get("result", {}).get("devices", []):
        hardware = device.get("hardwareProperties") or {}
        connection = device.get("connectionProperties") or {}
        properties = device.get("deviceProperties") or {}
        if hardware.get("platform") != "iOS" or hardware.get("reality") != "physical":
            continue
        if connection.get("transportType") != "wired" and connection.get("tunnelState") != (
            "connected"
        ):
            continue
        problems: list[Problem] = []
        if connection.get("pairingState") != "paired":
            problems.append("not_paired")
        if properties.get("developerModeStatus") != "enabled":
            problems.append("developer_mode_off")
        udid = str(hardware.get("udid", ""))
        if udid:
            found.append(Found("ios", udid, str(properties.get("name") or udid), tuple(problems)))
    return found


def parse_simctl(data: dict[str, Any]) -> list[Found]:
    """``xcrun simctl list devices booted -j``: the running simulators."""
    return [
        Found("ios", str(d["udid"]), str(d.get("name") or d["udid"]))
        for runtime in data.get("devices", {}).values()
        for d in runtime
        if d.get("state") == "Booted"
    ]


def _problems_of(
    device: Device, scan: Scan, found: dict[str, Found], not_responding: Collection[str]
) -> list[Problem]:
    if device.platform not in DRIVERS:
        return []  # a fake device is always ready
    problems: list[Problem] = []
    simulator = device.platform == "ios" and bool(SIMULATOR_UDID.match(device.native_id))
    listed = (
        scan.simulators
        if simulator
        else scan.android
        if device.platform == "android"
        else scan.iphones
    )
    if listed is None:
        problems.append("tool_missing")
    elif device.native_id not in {f.native_id for f in listed}:
        problems.append("disconnected")
    if device.native_id in found:
        problems.extend(found[device.native_id].problems)
    if isinstance(device, AppiumDevice) and scan.appium_up.get(device.appium_url) is False:
        problems.append("appium_unreachable")
    if scan.drivers is not None and DRIVERS[device.platform] not in scan.drivers:
        problems.append("driver_missing")
    if isinstance(device, IOSDevice) and not simulator and not device.signed:
        problems.append("wda_unsigned")
    if isinstance(device, AppiumDevice) and device.identity_changed:
        problems.append("identity_changed")
    if not problems and isinstance(device, AppiumDevice) and device.locked:
        problems.append("locked")  # the last look found its screen locked or dark
    if not problems and device.native_id in not_responding:
        problems.append("not_responding")  # connected, nothing missing, but it does not answer
    return problems


def report(
    devices: Sequence[Device], scan: Scan, not_responding: Collection[str] = ()
) -> list[wire.DeviceInfo]:
    """Every device with what it still needs (an empty list: nothing). ``not_responding``:
    the phones whose agent could not be started or kept (``Inventory``), reported as such
    while nothing else is wrong with them."""
    found = {f.native_id: f for f in [*(scan.android or []), *(scan.iphones or [])]}
    return [
        wire.DeviceInfo(
            native_id=d.native_id,
            platform=d.platform,
            name=d.name[:128],
            sensitive_apps=d.sensitive_apps,
            capabilities=d.capabilities,
            problems=_problems_of(d, scan, found, not_responding),
        )
        for d in devices
    ]


async def _run(*command: str, timeout: float = 20) -> bytes | None:
    """The command's output, or None when the tool is missing or fails."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (OSError, TimeoutError):
        return None
    return out if process.returncode == 0 else None


def _adb() -> str | None:
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    bundled = Path(home, "platform-tools", "adb") if home else None
    return shutil.which("adb") or (str(bundled) if bundled and bundled.exists() else None)


def _stamp(path: Path | None) -> tuple[int, int] | None:
    """When the configuration file was last written, and its size; None if it cannot be
    looked at (or there is none)."""
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


Build = Callable[[dict[str, Any], dict[str, int] | None], Device]
Configure = Callable[[Device, dict[str, Any]], None]


class Inventory:
    """The devices this Gateway drives: those its configuration names (settings keyed by
    ``native_id``: name, signing, sensitive apps) and every other one found connected.
    A device keeps its adapter and ports while the Gateway runs; ``devices`` only grows.

    The configuration is read again at every scan when it changed on disk: an entry
    that was added or changed (``setup`` signing an iPhone, say) is applied to the device's
    adapter there and then, so the next report says what the device needs now, without
    restarting the Gateway, and without touching the other devices."""

    def __init__(
        self,
        settings: Sequence[dict[str, Any]],
        *,
        build: Build,
        appium_url: str,
        scan: Callable[[], Any] | None = None,
        keep_warm: bool = False,
        warm_devices: bool = False,
        configure: Configure | None = None,
        settings_file: Path | None = None,
    ) -> None:
        """``keep_warm`` is the default for devices found without settings of their own;
        only a running Gateway (``warm_devices``) starts their agents: ``doctor`` and
        ``setup`` only look. ``settings_file`` is the configuration ``settings`` came from;
        when given, its ``[[devices]]`` are read again whenever it changes, and
        ``configure`` applies a changed entry to the device's adapter."""
        self.build = build
        self.configure = configure
        self.appium_url = appium_url
        self.keep_warm = keep_warm
        self.warm_devices = warm_devices
        self._settings_file = settings_file
        self._settings_seen = _stamp(settings_file)
        self._settings: dict[str, dict[str, Any]] = {str(s["native_id"]): dict(s) for s in settings}
        self._warming: dict[str, asyncio.Task[None]] = {}
        self._rechecking: dict[str, asyncio.Task[None]] = {}
        self._warm_failed_at: dict[str, float] = {}
        # Phones whose agent could not be started or kept: reported as ``not_responding``
        # until a later warm-up succeeds.
        self._not_responding: set[str] = set()
        # Set when that changes, so the new report goes out now, not at the next scan.
        self.changed = asyncio.Event()
        self.devices: list[Device] = []
        self._ports = Ports()
        self._scanner = Scanner(self.devices)
        self._scan = scan or self._scanner.scan
        self.last_scan: Scan | None = None  # what the latest refresh saw
        for spec in settings:
            self._add(dict(spec))

    def _add(self, spec: dict[str, Any]) -> None:
        self.devices.append(self.build(spec, self._ports.take(spec["platform"])))

    async def refresh(self) -> list[wire.DeviceInfo]:
        """Looks again; a device found for the first time gets its adapter and ports."""
        self._reload_settings()
        scan: Scan = await self._scan()
        self.last_scan = scan
        known = {d.native_id for d in self.devices}
        for f in [*(scan.android or []), *(scan.iphones or []), *(scan.simulators or [])]:
            if f.native_id not in known:
                known.add(f.native_id)
                self._add(
                    {
                        "platform": f.platform,
                        "native_id": f.native_id,
                        "name": f.name,
                        "appium_url": self.appium_url,
                        "keep_warm": self.keep_warm,
                    }
                )
        infos = report(self.devices, scan, self._not_responding)
        if self.warm_devices:
            self._warm(infos)
            self._recheck_locked()
        return infos

    def _reload_settings(self) -> None:
        """Applies the ``[[devices]]`` entries that changed in the configuration file since
        it was last read. An entry for a device not yet known adds it (as at start-up); an
        entry that was removed changes nothing until the Gateway restarts."""
        stamp = _stamp(self._settings_file)
        if self._settings_file is None or stamp == self._settings_seen:
            return
        self._settings_seen = stamp
        try:
            entries = tomllib.loads(self._settings_file.read_text()).get("devices", [])
        except (OSError, tomllib.TOMLDecodeError) as error:
            log.warning("not reading %s again: %s", self._settings_file, error)
            return
        by_device = {d.native_id: d for d in self.devices}
        for entry in entries:
            spec = dict(entry)
            native_id = str(spec.get("native_id", ""))
            if not native_id or self._settings.get(native_id) == spec:
                continue
            self._settings[native_id] = spec
            device = by_device.get(native_id)
            if device is None:
                self._add(spec)
            elif self.configure is not None:
                self.configure(device, spec)
            log.info("settings of %s applied from %s", native_id, self._settings_file.name)

    def _warm(self, infos: list[wire.DeviceInfo]) -> None:
        """Starts, in the background, what keeps each ``keep_warm`` phone's session open
        (``AppiumDevice.warm``): one at a time per phone, only for a phone that has nothing
        left to fix but, perhaps, not responding, and not again for KEEP_WARM_BACKOFF seconds
        after a failure."""
        now = time.monotonic()
        for device, info in zip(self.devices, infos, strict=True):
            if not isinstance(device, AppiumDevice) or not device.keep_warm:
                continue
            if any(problem not in ("not_responding", "locked") for problem in info.problems):
                continue
            running = self._warming.get(device.native_id)
            if running is not None and not running.done():
                continue
            failed = self._warm_failed_at.get(device.native_id)
            if failed is not None and now - failed < KEEP_WARM_BACKOFF:
                continue
            self._warming[device.native_id] = asyncio.create_task(self._keep(device))

    async def _keep(self, device: AppiumDevice) -> None:
        locked = device.locked
        try:
            await device.warm()
        except DeviceError as error:
            # A lost session is a clean signal: it is opened again by the next scan. A
            # locked or dark screen is reported as such and looked at again at the next
            # scan: looking is cheap. Anything else (a hung agent, an unreachable phone) is
            # left alone for a while, and the phone counts as not responding: it cannot
            # take tasks.
            if error.reason not in ("session_lost", "locked"):
                self._warm_failed_at[device.native_id] = time.monotonic()
                self._mark(device, not_responding=True)
            log.info("could not keep %s ready: %s", device.name, error.reason)
        else:
            self._mark(device, not_responding=False)
        if device.locked != locked:
            self.changed.set()

    def _recheck_locked(self) -> None:
        """Looks again, in the background, at each phone last seen locked that is not kept
        warm (those are looked at by ``_keep``), so that it shows as usable once someone
        unlocks it: it takes no task while locked, so nothing else would look. A phone
        whose session is gone cannot be looked at: it is no longer reported locked, and
        the next use finds out."""
        for device in self.devices:
            if not isinstance(device, AppiumDevice) or not device.locked or device.keep_warm:
                continue
            running = self._rechecking.get(device.native_id)
            if running is not None and not running.done():
                continue
            if not device.session_open:
                device.locked = False
                self.changed.set()
                continue
            self._rechecking[device.native_id] = asyncio.create_task(self._look(device))

    async def _look(self, device: AppiumDevice) -> None:
        with contextlib.suppress(DeviceError, TimeoutError):
            async with asyncio.timeout(RECHECK_TIMEOUT):
                await device.check_lock()
        if not device.locked:
            self.changed.set()

    def _mark(self, device: AppiumDevice, *, not_responding: bool) -> None:
        if (device.native_id in self._not_responding) == not_responding:
            return
        if not_responding:
            self._not_responding.add(device.native_id)
        else:
            self._not_responding.discard(device.native_id)
        self.changed.set()


class Scanner:
    """Looks at this machine with the platform tools."""

    def __init__(self, devices: Sequence[Device], http: httpx.AsyncClient | None = None):
        self.devices = devices  # the Inventory's list, to know which Appium servers to check
        # Appium runs on this computer (loopback only): never through a proxy.
        self.http = http or httpx.AsyncClient(timeout=3, trust_env=False)
        self._drivers: set[str] | None = None
        self._drivers_at = 0.0

    async def scan(self) -> Scan:
        adb = _adb()
        android_out = await _run(adb, "devices", "-l") if adb else None
        return Scan(
            android=None if android_out is None else parse_adb(android_out.decode()),
            iphones=await self._iphones(),
            simulators=await self._simulators(),
            appium_up=await self._appium(),
            drivers=await self._installed_drivers(),
        )

    async def _iphones(self) -> list[Found] | None:
        if shutil.which("xcrun") is None:
            return None
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "devices.json")
            if (
                await _run("xcrun", "devicectl", "list", "devices", "--json-output", str(path))
                is None
            ):
                return None
            try:
                return parse_devicectl(json.loads(path.read_text()))
            except (OSError, ValueError):
                return None

    async def _simulators(self) -> list[Found] | None:
        out = await _run("xcrun", "simctl", "list", "devices", "booted", "-j")
        try:
            return None if out is None else parse_simctl(json.loads(out))
        except ValueError:
            return None

    async def _appium(self) -> dict[str, bool]:
        up = {}
        for url in {d.appium_url for d in self.devices if isinstance(d, AppiumDevice)}:
            try:
                up[url] = (await self.http.get(f"{url}/status")).status_code == 200
            except httpx.HTTPError:
                up[url] = False
        return up

    async def _installed_drivers(self) -> set[str] | None:
        """Drivers of the Appium on this machine; unknown if Appium runs elsewhere."""
        if time.monotonic() - self._drivers_at < DRIVER_RECHECK and self._drivers is not None:
            return self._drivers
        if shutil.which("appium") is None:
            return None
        out = await _run("appium", "driver", "list", "--installed", "--json", timeout=60)
        try:
            self._drivers = None if out is None else set(json.loads(out))
        except ValueError:
            self._drivers = None
        self._drivers_at = time.monotonic()
        return self._drivers
