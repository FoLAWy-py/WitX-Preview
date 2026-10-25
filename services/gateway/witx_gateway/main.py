"""`witx-gateway --config gateway.toml [enroll [--replace] | doctor | setup]`

``enroll`` reads the one-time code from ``witx admin add-gateway`` and stores the
Gateway's credential in the system key store (see ``credentials.py``). ``doctor`` says
what this computer and each connected device still need, and how to fix it; ``setup``
fixes what a program can, on this computer, after the person at it agrees.

See ``services/gateway/gateway.example.toml`` for a full example.
The Gateway drives every device it finds connected (``discovery.Inventory``); an owner
enables each one on the Web app before it can take tasks. ``[[devices]]`` entries are
optional settings for one device, matched by ``platform`` and ``native_id``: ``name``,
``sensitive_apps`` (every input in these apps counts as sensitive), ``appium_url`` and
``capabilities`` (extra Appium capabilities, such as WebDriverAgent signing on a real
iPhone); iOS also takes ``wda_url``. A ``fake`` device exists only through its entry.
The Gateway opens, reopens and closes the Appium sessions itself and gives every device
its own local ports (``ports.Ports``). ``[[devices]]`` entries added or changed while it
runs (``setup`` signing an iPhone) are applied at its next scan; ``appium_url``
and the ports need a restart.
"""

import argparse
import asyncio
import contextlib
import getpass
import signal
import socket
import sys
import tomllib
from pathlib import Path
from typing import Any

import httpx
from witx_contracts import gateway as wire
from witx_contracts import logs

from witx_gateway import doctor, setup
from witx_gateway.client import GatewayClient, MissingCredential, checked_cloud_url
from witx_gateway.credentials import CredentialStore
from witx_gateway.devices.android import AndroidDevice
from witx_gateway.devices.appium import AppiumDevice, Identities
from witx_gateway.devices.base import Device
from witx_gateway.devices.fake import FakeDevice
from witx_gateway.devices.ios import IOSDevice
from witx_gateway.discovery import Inventory
from witx_gateway.journal import Journal
from witx_gateway.ports import DEFAULT_APPIUM_URL


def _settings(spec: dict[str, Any]) -> dict[str, Any]:
    """The settings of one ``[[devices]]`` entry that can change while the Gateway runs."""
    return {
        "name": spec.get("name", spec["native_id"]),
        "sensitive_apps": list(spec.get("sensitive_apps", [])),
        # Extra Appium capabilities for sessions the Gateway opens (e.g. signing).
        "capabilities": dict(spec.get("capabilities", {})),
        "keep_warm": bool(spec.get("keep_warm", False)),
    }


def build_device(
    spec: dict[str, Any],
    ports: dict[str, int] | None = None,
    identities: Identities | None = None,
) -> Device:
    platform = spec["platform"]
    settings = _settings(spec)
    appium = {
        "appium_url": spec.get("appium_url", DEFAULT_APPIUM_URL),
        "ports": ports,
        "identities": identities,
    }
    if platform == "android":
        return AndroidDevice(serial=spec["native_id"], **appium, **settings)
    if platform == "ios":
        return IOSDevice(udid=spec["native_id"], wda_url=spec.get("wda_url"), **appium, **settings)
    if platform == "fake":
        return FakeDevice(
            native_id=spec["native_id"],
            name=settings["name"],
            sensitive_apps=settings["sensitive_apps"],
        )
    raise ValueError(f"unknown platform: {platform}")


def configure_device(device: Device, spec: dict[str, Any]) -> None:
    """Applies a changed ``[[devices]]`` entry to the device's adapter. The Appium URL
    and the ports are tied to the adapter: those take effect when the Gateway restarts."""
    if isinstance(device, IOSDevice):
        device.configure(wda_url=spec.get("wda_url"), **_settings(spec))
    elif isinstance(device, AppiumDevice):
        device.configure(**_settings(spec))


def _cloud_url(config: dict[str, Any]) -> str:
    try:
        return checked_cloud_url(
            config["cloud_url"], allow_insecure=bool(config.get("allow_insecure_cloud", False))
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None


def journal_path(config: dict[str, Any], path: Path) -> Path:
    """The journal; a relative path is read from the config file's folder. The credential
    is keyed by the journal's absolute path, so ``enroll`` run by hand and a service that
    starts in / (launchd, systemd) must find the same one."""
    return (path.parent / str(config.get("journal", "witx-gateway.sqlite3"))).absolute()


def load(path: Path) -> tuple[GatewayClient, Journal]:
    config = tomllib.loads(path.read_text())
    journal_file = journal_path(config, path)
    journal = Journal(journal_file)
    inventory = Inventory(
        config.get("devices", []),
        build=lambda spec, ports: build_device(spec, ports, journal),
        configure=configure_device,
        appium_url=config.get("appium_url", DEFAULT_APPIUM_URL),
        keep_warm=bool(config.get("keep_warm", False)),
        warm_devices=True,
        settings_file=path,
    )
    try:
        client = GatewayClient(
            cloud_url=_cloud_url(config),
            credentials=CredentialStore(journal_file),
            journal=journal,
            devices=inventory.devices,
            scan=inventory.refresh,
            rescan=inventory.changed,
        )
    except MissingCredential:
        journal.close()
        raise SystemExit(
            f"not enrolled yet: run `witx-gateway --config {path} enroll` with the code "
            "from the Devices page (Add device pool) or `witx admin add-gateway`"
        ) from None
    return client, journal


def enroll(path: Path, code: str, *, replace: bool) -> None:
    """Spends the one-time code and stores the credential it returns."""
    config = tomllib.loads(path.read_text())
    store = CredentialStore(journal_path(config, path))
    if store.load() is not None and not replace:
        raise SystemExit("this Gateway is already enrolled; pass --replace to enroll it anew")
    response = httpx.post(
        f"{_cloud_url(config)}/gateway/v1/enroll",
        json=wire.Enrollment(code=code, host=socket.gethostname()).model_dump(),
        timeout=30,
    )
    if response.status_code == 403:
        raise SystemExit("the code was not accepted (unknown, already used or expired)")
    response.raise_for_status()
    credential = wire.Credential.model_validate_json(response.content)
    store.save(credential.credential)
    print(f"enrolled as gateway {credential.gateway_id}")


async def _run(path: Path) -> None:
    client, journal = load(path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await client.run(stop)
    finally:
        for device in client.devices:
            with contextlib.suppress(Exception):
                await device.close()  # end the Appium sessions this Gateway opened
        journal.close()


async def _doctor(path: Path) -> int:
    config = tomllib.loads(path.read_text())
    appium_url = config.get("appium_url", DEFAULT_APPIUM_URL).rstrip("/")
    journal = Journal(journal_path(config, path))
    inventory = Inventory(
        config.get("devices", []),
        build=lambda spec, ports: build_device(spec, ports, journal),
        appium_url=appium_url,
    )
    devices = await inventory.refresh()
    assert inventory.last_scan is not None
    checks = doctor.computer_checks(
        inventory.last_scan,
        config=path,
        appium_url=appium_url,
        appium_up=await doctor.reachable(f"{appium_url}/status", proxies=False),
        enrolled=CredentialStore(journal_path(config, path)).load() is not None,
        cloud_url=config["cloud_url"],
        cloud_up=await doctor.reachable(f"{config['cloud_url'].rstrip('/')}/health", proxies=True),
        macos=doctor.macos(),
    )
    report, ready = doctor.render(checks, devices)
    print(report)
    return 0 if ready else 1


async def _setup(path: Path, *, yes: bool, boot: list[str], team: str | None) -> int:
    """Fixes what ``doctor`` finds and a program can fix, after the person here agrees."""
    config = tomllib.loads(path.read_text())
    appium_url = config.get("appium_url", DEFAULT_APPIUM_URL).rstrip("/")
    journal = Journal(journal_path(config, path))
    inventory = Inventory(
        config.get("devices", []),
        build=lambda spec, ports: build_device(spec, ports, journal),
        appium_url=appium_url,
    )
    devices = await inventory.refresh()
    journal.close()
    assert inventory.last_scan is not None
    result = setup.plan(
        scan=inventory.last_scan,
        devices=devices,
        machine=await setup.inspect(),
        appium_url=appium_url,
        appium_up=await doctor.reachable(f"{appium_url}/status", proxies=False),
        config=path,
        configured={str(d["native_id"]) for d in config.get("devices", [])},
        log_dir=journal_path(config, path).resolve().parent,
        boot=boot,
        team=team,
    )
    print(setup.render(result))
    if result.steps:
        if not yes:
            if not sys.stdin.isatty():
                raise SystemExit("not a terminal: pass --yes to run these steps")
            answer = input("Run these steps on this computer? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                return 1
        if not await setup.execute(result):
            return 1
        print("\nChecking again:")
    return await _doctor(path)


def main() -> None:
    logs.configure("gateway")
    parser = argparse.ArgumentParser(prog="witx-gateway")
    parser.add_argument("--config", type=Path, required=True)
    sub = parser.add_subparsers(dest="command")
    p_enroll = sub.add_parser("enroll", help="enroll with a one-time code (read from stdin)")
    p_enroll.add_argument("--replace", action="store_true", help="replace an existing enrollment")
    sub.add_parser("doctor", help="check this computer and its devices; changes nothing")
    p_setup = sub.add_parser(
        "setup",
        help="fix what doctor finds and a program can fix: install adb, Node, Appium and its "
        "drivers, start Appium, boot a simulator, sign and build WebDriverAgent (asks first)",
    )
    p_setup.add_argument("--yes", action="store_true", help="do not ask before running")
    p_setup.add_argument(
        "--boot", action="append", default=[], help="boot this iOS simulator or Android AVD"
    )
    p_setup.add_argument("--team", help="the Apple team ID to sign with, when there are several")
    p_forget = sub.add_parser(
        "forget-device",
        help="accept the device now connected as NATIVE_ID (after it was refused because "
        "another device answered than the one first seen)",
    )
    p_forget.add_argument("native_id")
    args = parser.parse_args()
    if args.command == "forget-device":
        config = tomllib.loads(args.config.read_text())
        journal = Journal(journal_path(config, args.config))
        forgot = journal.forget_identity(args.native_id)
        journal.close()
        print(
            f"{args.native_id}: the next device to answer is accepted"
            if forgot
            else f"{args.native_id}: no identity was kept"
        )
        return
    if args.command == "doctor":
        sys.exit(asyncio.run(_doctor(args.config)))
    if args.command == "setup":
        sys.exit(asyncio.run(_setup(args.config, yes=args.yes, boot=args.boot, team=args.team)))
    if args.command == "enroll":
        # Not an argument: it would stay in the shell history and the process list.
        code = getpass.getpass("Enrollment code: ") if sys.stdin.isatty() else sys.stdin.readline()
        enroll(args.config, code.strip(), replace=args.replace)
    else:
        asyncio.run(_run(args.config))
