"""`witx-gateway --config gateway.toml doctor`: what this computer and each connected device
still need before tasks can run, and the command that fixes each. It only looks; it never
installs or changes anything (device pool, #33). ``setup`` does the fixing."""

import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from witx_contracts import gateway as wire
from witx_contracts.common import Problem

from witx_gateway.discovery import DRIVERS, Scan

FIXES: dict[Problem, str] = {
    "disconnected": "connect it to this computer (a simulator: xcrun simctl boot <udid>)",
    "tool_missing": "install Android platform-tools (adb) or Xcode, see the checks above",
    "adb_unauthorized": "unlock the phone and allow USB debugging for this computer",
    "adb_offline": "reconnect the cable, or: adb kill-server && adb devices",
    "not_paired": "unlock the iPhone and tap “Trust This Computer”",
    "developer_mode_off": "iPhone: Settings > Privacy & Security > Developer Mode, then restart",
    "appium_unreachable": "start Appium, see the checks above",
    "driver_missing": "install the Appium driver, see the checks above",
    "wda_unsigned": "run `witx-gateway --config <config> setup` (signs WebDriverAgent with "
    "your Apple team), or add signing to its [[devices]] entry (see gateway.example.toml); "
    "a running Gateway picks it up within seconds, no restart",
    "identity_changed": "another device answers than the one first seen; if it is the one "
    "you mean now: witx-gateway --config <config> forget-device <native_id>",
    "not_responding": "the phone does not answer: unlock it, check the cable or Wi-Fi, or "
    "unplug it and plug it in again",
    "locked": "the screen is locked or dark: unlock the phone; for a phone kept for the pool, "
    "set Auto-Lock to Never (a takeover tries waking it)",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    fix: str = ""


def computer_checks(
    scan: Scan,
    *,
    config: Path,
    appium_url: str,
    appium_up: bool,
    enrolled: bool,
    cloud_url: str,
    cloud_up: bool,
    macos: bool,
) -> list[Check]:
    """What this computer needs, whatever devices are connected."""
    port = urlsplit(appium_url).port or 4723
    checks = [
        Check(
            "adb (Android)",
            scan.android is not None,
            "install Android platform-tools and set ANDROID_HOME (or put adb on PATH)",
        )
    ]
    if macos:
        checks.append(
            Check(
                "Xcode command-line tools (iOS)",
                scan.iphones is not None and scan.simulators is not None,
                "install Xcode, then: sudo xcode-select -s /Applications/Xcode.app",
            )
        )
    checks.append(
        Check(
            f"Appium at {appium_url}",
            appium_up,
            f"appium --address 127.0.0.1 --port {port} --log-level warn",
        )
    )
    for platform, driver in DRIVERS.items():
        if platform == "ios" and not macos:
            continue
        checks.append(
            Check(
                f"Appium driver {driver}",
                scan.drivers is not None and driver in scan.drivers,
                f"appium driver install {driver}"
                if scan.drivers is not None
                else "install Appium: npm install -g appium",
            )
        )
    checks.append(
        Check(
            "Enrolled with the cloud",
            enrolled,
            f"witx-gateway --config {config} enroll  (code from `witx admin add-gateway`)",
        )
    )
    checks.append(Check(f"Cloud at {cloud_url}", cloud_up, "check cloud_url and the network"))
    return checks


def render(checks: list[Check], devices: list[wire.DeviceInfo]) -> tuple[str, bool]:
    """The report, and whether everything is ready."""
    lines = ["This computer:"]
    for check in checks:
        lines.append(f"  {'ok ' if check.ok else 'FIX'}  {check.name}")
        if not check.ok:
            lines.append(f"         → {check.fix}")
    lines.append("Devices:" if devices else "Devices: none connected")
    for device in devices:
        state = "ok " if not device.problems else "FIX"
        lines.append(f"  {state}  {device.name} ({device.platform}, {device.native_id})")
        for problem in device.problems:
            lines.append(f"         → {problem}: {FIXES[problem]}")
    ready = all(c.ok for c in checks) and all(not d.problems for d in devices)
    if devices:
        lines.append(
            "A ready device still takes tasks only after an owner enables it on the Web app."
        )
    return "\n".join(lines), ready


async def reachable(url: str, *, proxies: bool) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=proxies) as http:
            return (await http.get(url)).status_code == 200
    except httpx.HTTPError:
        return False


def macos() -> bool:
    return sys.platform == "darwin"
