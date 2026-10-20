"""`witx-gateway --config gateway.toml setup`: fixes what this computer and its devices
still need and a program can fix (device pool, #134), on this computer, after the person
at it confirms.

``doctor`` only looks. ``setup`` turns the same findings into a plan of steps and runs
them: install adb, Node, Appium and its drivers, start Appium, boot a simulator, and for
a real iPhone sign and build WebDriverAgent with the team of this computer's Apple
Development certificate. Nothing here comes from the cloud and nothing runs through a
shell: every step is a fixed argument list, and the only values put into one (a UDID, a
team ID, a bundle ID, a simulator name) are checked first. What a person must do on the
phone ("Trust This Computer", Developer Mode, USB debugging, trusting the developer
certificate) is only told.

A Gateway that is running picks the signing entry up at its next scan: the iPhone
becomes usable without restarting it, and the other devices it drives are not touched.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from witx_contracts import gateway as wire

from witx_gateway.discovery import DRIVERS, Scan

APPIUM_PACKAGE = "appium@3"  # the major version the Gateway is tested with
SIGNING_IDENTITY = "Apple Development"
WDA_SCHEME = "WebDriverAgentRunner"
START_WAIT = 60.0  # seconds to wait for a server or a phone that was started

UDID = re.compile(r"^[0-9A-Fa-f-]{20,40}$")
TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
BUNDLE_ID = re.compile(r"^[A-Za-z0-9.-]{3,100}$")
AVD_NAME = re.compile(r"^[\w.-]{1,80}$")


@dataclass(frozen=True)
class Step:
    """One thing to do. ``argv`` is run as it is, never through a shell. ``append`` adds
    text to a file instead (the device's signing settings in the Gateway's config).
    ``keep_running`` leaves the process running after setup (Appium, an emulator): its
    output goes to ``log``, ``until`` is polled until it answers, and ``env`` is added to
    its environment."""

    name: str
    argv: tuple[str, ...] = ()
    append: tuple[Path, str] | None = None
    keep_running: bool = False
    log: Path | None = None
    until: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    cwd: Path | None = None
    timeout: float = 1800.0


@dataclass(frozen=True)
class Plan:
    steps: list[Step] = field(default_factory=list)
    # What a person has to do, or setup could not decide, with the reason.
    todo: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Machine:
    """What this computer has, looked at once."""

    macos: bool
    brew: bool
    node: bool
    appium: bool
    emulator: str | None  # the Android emulator tool
    wda_dir: Path | None  # the WebDriverAgent project inside the xcuitest driver
    teams: tuple[str, ...]  # team IDs of the Apple Development certificates in the key store
    simulators: tuple[tuple[str, str], ...] = ()  # (udid, name) of iOS simulators not running
    avds: tuple[str, ...] = ()
    # The Android SDK, which Appium's Android driver cannot open a session without.
    android_home: Path | None = None


# ------------------------------------------------------------------ reading this computer


def parse_identities(text: str) -> list[str]:
    """Names of the valid Apple Development signing identities, from ``security
    find-identity -v -p codesigning``."""
    names = re.findall(r'^\s*\d+\)\s+[0-9A-F]{40}\s+"([^"]+)"', text, re.MULTILINE)
    return [n for n in names if n.startswith(("Apple Development", "iPhone Developer"))]


def team_of(subject: str) -> str | None:
    """The team ID of a certificate: the ``OU`` of its subject. (The ID in brackets in the
    certificate's name is the certificate's own, not the team's.)"""
    found = re.search(r"OU\s*=\s*([A-Z0-9]{10})\b", subject)
    return found.group(1) if found else None


def parse_simulators(data: dict[str, object]) -> list[tuple[str, str]]:
    """The iOS simulators that exist and are not running, from ``simctl list devices
    available -j``."""
    out: list[tuple[str, str]] = []
    devices = data.get("devices")
    if not isinstance(devices, dict):
        return out
    for runtime, entries in devices.items():
        if "iOS" not in runtime or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("state") == "Shutdown":
                out.append((str(entry["udid"]), str(entry["name"])))
    return out


async def _output(*argv: str, timeout: float = 60) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (OSError, TimeoutError):
        return None
    return out.decode(errors="replace") if process.returncode == 0 else None


def _android_home() -> Path | None:
    """The Android SDK: where the environment says, else the one the adb on PATH is in."""
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if home:
        return Path(home)
    adb = shutil.which("adb")
    sdk = Path(adb).resolve().parent.parent if adb else None
    return sdk if sdk and Path(sdk, "platform-tools", "adb").exists() else None


def _emulator() -> str | None:
    home = _android_home()
    bundled = Path(home, "emulator", "emulator") if home else None
    return shutil.which("emulator") or (str(bundled) if bundled and bundled.exists() else None)


async def inspect() -> Machine:
    """Looks at this computer with its own tools."""
    macos = sys.platform == "darwin"
    wda_dir: Path | None = None
    if shutil.which("appium"):
        listed = await _output("appium", "driver", "list", "--installed", "--json")
        try:
            path = json.loads(listed or "{}").get("xcuitest", {}).get("installPath")
        except (ValueError, AttributeError):
            path = None
        if path:
            project = Path(path, "node_modules", "appium-webdriveragent")
            wda_dir = project if Path(project, "WebDriverAgent.xcodeproj").exists() else None
    teams: list[str] = []
    simulators: list[tuple[str, str]] = []
    if macos:
        identities = parse_identities(
            await _output("security", "find-identity", "-v", "-p", "codesigning") or ""
        )
        for name in identities:
            pem = await _output("security", "find-certificate", "-c", name, "-p")
            if pem is None:
                continue
            subject = await _subject(pem)
            team = team_of(subject or "")
            if team and team not in teams:
                teams.append(team)
        listed_sims = await _output("xcrun", "simctl", "list", "devices", "available", "-j")
        try:
            simulators = parse_simulators(json.loads(listed_sims or "{}"))
        except ValueError:
            simulators = []
    emulator = _emulator()
    avds: list[str] = []
    if emulator:
        avds = (await _output(emulator, "-list-avds") or "").split()
    return Machine(
        macos=macos,
        brew=shutil.which("brew") is not None,
        node=shutil.which("node") is not None and shutil.which("npm") is not None,
        appium=shutil.which("appium") is not None,
        emulator=emulator,
        android_home=_android_home(),
        wda_dir=wda_dir,
        teams=tuple(teams),
        simulators=tuple(simulators),
        avds=tuple(avds),
    )


async def _subject(pem: str) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "openssl",
            "x509",
            "-noout",
            "-subject",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(pem.encode()), 20)
    except (OSError, TimeoutError):
        return None
    return out.decode(errors="replace") if process.returncode == 0 else None


# ------------------------------------------------------------------ planning


def signing_block(*, udid: str, name: str, team: str, bundle: str) -> str:
    """The ``[[devices]]`` entry that signs WebDriverAgent for one real iPhone."""
    return (
        "\n[[devices]]\n"
        'platform = "ios"\n'
        f'native_id = "{udid}"\n'
        f"name = {json.dumps(name, ensure_ascii=False)}\n"
        'capabilities = { "appium:xcodeOrgId" = "' + team + '", '
        '"appium:xcodeSigningId" = "' + SIGNING_IDENTITY + '", '
        '"appium:updatedWDABundleId" = "' + bundle + '" }\n'
    )


def bundle_id(team: str) -> str:
    """A WebDriverAgent bundle ID of the team's own: bundle IDs are unique across Apple
    accounts, so the shared default (com.facebook...) cannot be signed by another team."""
    return f"com.witx.webdriveragentrunner.{team.lower()}"


def resolve_boot(target: str, machine: Machine) -> tuple[str, str] | None:
    """The simulator or emulator a ``--boot`` argument names, by UDID or name: ``("ios",
    udid)`` or ``("android", avd)``."""
    wanted = target.strip().lower()
    for udid, name in machine.simulators:
        if wanted in (udid.lower(), name.lower()):
            return "ios", udid
    for avd in machine.avds:
        if wanted == avd.lower():
            return "android", avd
    return None


def plan(
    *,
    scan: Scan,
    devices: Sequence[wire.DeviceInfo],
    machine: Machine,
    appium_url: str,
    appium_up: bool,
    config: Path,
    configured: set[str],
    log_dir: Path,
    boot: Sequence[str] = (),
    team: str | None = None,
) -> Plan:
    """What to do about what is missing. ``configured`` are the ``native_id`` of the
    devices that already have a ``[[devices]]`` entry, which setup does not rewrite."""
    steps: list[Step] = []
    todo: list[str] = []

    def install(name: str, brew: tuple[str, ...], manual: str) -> None:
        if machine.macos and machine.brew:
            steps.append(Step(name, ("brew", *brew)))
        else:
            todo.append(f"{name}: {manual}")

    if scan.android is None:
        install(
            "Install Android platform-tools (adb)",
            ("install", "--cask", "android-platform-tools"),
            "install Android platform-tools and put adb on PATH or set ANDROID_HOME",
        )
    if machine.macos and (scan.iphones is None or scan.simulators is None):
        todo.append(
            "Xcode command-line tools: install Xcode from the App Store, open it once, then: "
            "sudo xcode-select -s /Applications/Xcode.app"
        )
    appium_present = machine.appium
    if not machine.appium:
        if not machine.node:
            install("Install Node.js", ("install", "node"), "install Node.js (with npm)")
        if machine.node or (machine.macos and machine.brew):
            steps.append(Step("Install Appium", ("npm", "install", "-g", APPIUM_PACKAGE)))
            appium_present = True
    if appium_present:
        for platform, driver in DRIVERS.items():
            if platform == "ios" and not machine.macos:
                continue
            unknown = scan.drivers is None
            missing = not machine.appium or (not unknown and driver not in (scan.drivers or ()))
            if missing:
                steps.append(
                    Step(f"Install Appium driver {driver}", ("appium", "driver", "install", driver))
                )
    host = urlsplit(appium_url)
    if not appium_up and appium_present:
        if host.hostname in ("127.0.0.1", "localhost", "::1") and host.port:
            steps.append(
                Step(
                    "Start Appium",
                    (
                        "appium",
                        "--address",
                        "127.0.0.1",
                        "--port",
                        str(host.port),
                        "--log-level",
                        "warn",
                    ),
                    keep_running=True,
                    log=log_dir / "appium.log",
                    until=f"{appium_url.rstrip('/')}/status",
                    env=(("ANDROID_HOME", str(machine.android_home)),)
                    if machine.android_home
                    else (),
                )
            )
        else:
            todo.append(f"Appium at {appium_url} is not on this computer: start it there")
    for target in boot:
        found = resolve_boot(target, machine)
        if found is None:
            todo.append(f"--boot {target}: no iOS simulator or Android AVD of that name or UDID")
        elif found[0] == "ios":
            steps.append(Step(f"Boot simulator {target}", ("xcrun", "simctl", "boot", found[1])))
        elif machine.emulator and AVD_NAME.match(found[1]):
            steps.append(
                Step(
                    f"Start emulator {found[1]}",
                    (machine.emulator, "-avd", found[1]),
                    keep_running=True,
                    log=log_dir / f"emulator-{found[1]}.log",
                )
            )
    unsigned = [d for d in devices if "wda_unsigned" in d.problems]
    if unsigned:
        steps.extend(_signing_steps(unsigned, machine, config, configured, team, todo))
    for device in devices:
        for problem, advice in _PERSON.items():
            if problem in device.problems:
                todo.append(f"{device.name}: {advice}")
    return Plan(steps, todo)


_PERSON = {
    "adb_unauthorized": "unlock the phone and allow USB debugging for this computer",
    "not_paired": "unlock the iPhone and tap “Trust This Computer”",
    "developer_mode_off": "iPhone: Settings > Privacy & Security > Developer Mode, then restart",
}


def _signing_steps(
    unsigned: Sequence[wire.DeviceInfo],
    machine: Machine,
    config: Path,
    configured: set[str],
    team: str | None,
    todo: list[str],
) -> list[Step]:
    if team is not None and not TEAM_ID.match(team):
        todo.append(f"--team {team}: a team ID is 10 capital letters and digits")
        return []
    teams = [team] if team else list(machine.teams)
    if not teams:
        todo.append(
            "WebDriverAgent signing: no Apple Development certificate in the key store; "
            "open Xcode > Settings > Accounts, sign in and create one"
        )
        return []
    if len(teams) > 1:
        todo.append(
            "WebDriverAgent signing: several teams found ("
            + ", ".join(teams)
            + "); say which with --team"
        )
        return []
    if machine.wda_dir is None:
        todo.append(
            "WebDriverAgent signing: the xcuitest driver is not installed, so there is no "
            "WebDriverAgent to build; run setup again after it is"
        )
        return []
    chosen = teams[0]
    bundle = bundle_id(chosen)
    steps: list[Step] = []
    for device in unsigned:
        if not UDID.match(device.native_id) or not BUNDLE_ID.match(bundle):
            todo.append(f"{device.name}: its ID is not one setup can use")
            continue
        # Build first, write the entry after: a running Gateway reads the entry as soon as
        # it is written and counts the iPhone as signed, so WebDriverAgent must be on
        # the phone by then; and a build that fails leaves the configuration as it was.
        steps.append(
            Step(
                f"Build WebDriverAgent for {device.name} (needs Xcode signed in to the team)",
                (
                    "xcodebuild",
                    "build-for-testing",
                    "-project",
                    str(machine.wda_dir / "WebDriverAgent.xcodeproj"),
                    "-scheme",
                    WDA_SCHEME,
                    "-destination",
                    f"id={device.native_id}",
                    "-allowProvisioningUpdates",
                    f"DEVELOPMENT_TEAM={chosen}",
                    f"CODE_SIGN_IDENTITY={SIGNING_IDENTITY}",
                    f"PRODUCT_BUNDLE_IDENTIFIER={bundle}",
                ),
                cwd=machine.wda_dir,
            )
        )
        if device.native_id in configured:
            todo.append(
                f"{device.name}: {config} already has an entry for it; add to its capabilities "
                f'"appium:xcodeOrgId" = "{chosen}", "appium:xcodeSigningId" = '
                f'"{SIGNING_IDENTITY}", "appium:updatedWDABundleId" = "{bundle}"'
            )
        else:
            steps.append(
                Step(
                    f"Sign WebDriverAgent for {device.name} in {config.name} (team {chosen})",
                    append=(
                        config,
                        signing_block(
                            udid=device.native_id,
                            name=device.name,
                            team=chosen,
                            bundle=bundle,
                        ),
                    ),
                )
            )
    if steps:
        todo.append(
            "WebDriverAgent on an iPhone: the first time, trust the developer certificate on "
            "the phone: Settings > General > VPN & Device Management"
        )
    return steps


# ------------------------------------------------------------------ running

Runner = Callable[[Step], Awaitable[tuple[bool, str]]]


def render(result: Plan) -> str:
    lines = ["Setup would:" if result.steps else "Setup has nothing it can do."]
    for number, step in enumerate(result.steps, 1):
        lines.append(f"  {number}. {step.name}")
        if step.argv:
            lines.append("       $ " + " ".join(step.argv))
        if step.append:
            lines.append(f"       adds to {step.append[0]}")
    if result.todo:
        lines.append("Left to you:")
        lines.extend(f"  - {item}" for item in result.todo)
    return "\n".join(lines)


async def run_step(step: Step) -> tuple[bool, str]:
    """Runs one step; whether it worked, and what to tell if it did not."""
    if step.append is not None:
        path, text = step.append
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as error:
            return False, str(error)
        return True, ""
    if step.keep_running:
        return await _start(step)
    try:
        process = await asyncio.create_subprocess_exec(
            *step.argv,
            cwd=step.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(process.communicate(), step.timeout)
    except OSError as error:
        return False, f"cannot run {step.argv[0]}: {error}"
    except TimeoutError:
        return False, "timed out"
    tail = "\n".join(out.decode(errors="replace").strip().splitlines()[-15:])
    return process.returncode == 0, tail


async def _start(step: Step) -> tuple[bool, str]:
    assert step.log is not None
    step.log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with step.log.open("ab") as log:
            subprocess.Popen(
                step.argv,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, **dict(step.env)},
            )
    except OSError as error:
        return False, f"cannot run {step.argv[0]}: {error}"
    if step.until is None:
        return True, ""
    deadline = time.monotonic() + START_WAIT
    async with httpx.AsyncClient(timeout=3, trust_env=False) as http:
        while time.monotonic() < deadline:
            try:
                if (await http.get(step.until)).status_code == 200:
                    return True, ""
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    return False, f"it did not answer at {step.until} in {START_WAIT:.0f} s; see {step.log}"


async def execute(
    result: Plan,
    *,
    run: Runner = run_step,
    say: Callable[[str], None] = print,
) -> bool:
    """Runs the steps in order and stops at the first that fails. Whether all worked."""
    for number, step in enumerate(result.steps, 1):
        say(f"[{number}/{len(result.steps)}] {step.name}")
        ok, detail = await run(step)
        if not ok:
            say(f"  failed: {detail}" if detail else "  failed")
            return False
    return True
