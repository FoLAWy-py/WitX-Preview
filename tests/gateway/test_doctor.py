"""`witx-gateway doctor`: what this computer and each device still need, with the fix."""

from pathlib import Path

from witx_contracts import gateway as wire
from witx_gateway import doctor
from witx_gateway.discovery import Scan


def checks(scan: Scan, **over: object) -> list[doctor.Check]:
    facts: dict[str, object] = {
        "config": Path("gateway.toml"),
        "appium_url": "http://127.0.0.1:4723",
        "appium_up": True,
        "enrolled": True,
        "cloud_url": "http://127.0.0.1:8000",
        "cloud_up": True,
        "macos": True,
    }
    return doctor.computer_checks(scan, **{**facts, **over})  # type: ignore[arg-type]


READY = Scan(android=[], iphones=[], simulators=[], drivers={"uiautomator2", "xcuitest"})


def test_a_ready_computer_and_device_need_nothing() -> None:
    phone = wire.DeviceInfo(native_id="A", platform="android", name="Pixel", capabilities=[])
    report, ready = doctor.render(checks(READY), [phone])
    assert ready and "FIX" not in report and "an owner enables it" in report


def test_each_missing_piece_comes_with_the_command_that_fixes_it() -> None:
    scan = Scan(android=None, iphones=[], simulators=[], drivers={"xcuitest"})
    found = checks(scan, appium_up=False, enrolled=False)
    fixes = {c.name: c.fix for c in found if not c.ok}
    assert set(fixes) == {
        "adb (Android)",
        "Appium at http://127.0.0.1:4723",
        "Appium driver uiautomator2",
        "Enrolled with the cloud",
    }
    assert fixes["Appium driver uiautomator2"] == "appium driver install uiautomator2"
    assert "enroll" in fixes["Enrolled with the cloud"]
    unauthorized = wire.DeviceInfo(
        native_id="B",
        platform="android",
        name="TDT",
        capabilities=[],
        problems=["adb_unauthorized"],
    )
    report, ready = doctor.render(found, [unauthorized])
    assert not ready and "allow USB debugging" in report


def test_off_a_mac_the_ios_tools_are_not_asked_for() -> None:
    names = [c.name for c in checks(READY, macos=False)]
    assert "Xcode command-line tools (iOS)" not in names and "Appium driver xcuitest" not in names
