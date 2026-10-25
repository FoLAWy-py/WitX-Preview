"""Devices: when one can work, and when a person takes one over."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from witx.domain.model import (
    Device,
    Lease,
    Run,
    WaitReason,
)
from witx.domain.rules.common import Rejected
from witx.domain.rules.schedules import SCHEDULE_GRACE

# A takeover ends after this long without input from the person.
TAKEOVER_IDLE = timedelta(minutes=2)


# The phone's side keys: only a person taking it over presses them. The agent has no tool
# for them and is not told the phone has them; an input of the agent's that names one is
# refused. Locking the phone, above all, would end its own work.
PERSON_ONLY_KINDS = frozenset({"volume_up", "volume_down", "lock"})


# What a person may do during a takeover: a click, a held click and a drag on the picture,
# typed text, the system buttons and the side keys.
MANUAL_INPUT_KINDS = (
    frozenset({"tap", "long_press", "swipe", "type_text", "back", "home"}) | PERSON_ONLY_KINDS
)


def takeover_active(lease: Lease, now: datetime) -> bool:
    """Whether a person controls the lease's device right now."""
    return (
        lease.takeover_id is not None
        and lease.takeover_expires_at is not None
        and lease.takeover_expires_at > now
    )


def held_by_nobody(lease: Lease | None, now: datetime) -> bool:
    """No task holds the device and no one controls it. A person's lease whose takeover
    ran out (its connection was lost with the process that served it) holds nothing."""
    return lease is None or (lease.task_id is None and not takeover_active(lease, now))


def admit_device_takeover(device: Device, lease: Lease | None, now: datetime) -> None:
    """A person may take over a device outside any task when it is enabled, usable and
    free. A device a task holds is taken over from that task, which pauses first."""
    if not device.enabled:
        raise Rejected("device_not_enabled", f"{device.name}: an owner enables it first")
    require_usable(device, takeover=True)
    if lease is not None and lease.task_id is not None:
        raise Rejected("device_busy", "a task holds this phone: take it over from the task")
    if not held_by_nobody(lease, now):
        raise Rejected("takeover_active", "someone else controls this phone")


# A run none of whose phones can be used waits this long for one to come back, then
# fails: as long as a scheduled run may start late.
DEVICE_WAIT = SCHEDULE_GRACE


def usable(device: Device) -> bool:
    """Whether a run can use the device now: its Gateway is connected and reports nothing
    missing (a phone that is plugged in but does not answer reports ``not_responding``)."""
    return device.online and not device.problems


# What does not keep a person from taking a phone over: its locked or dark screen, which
# the Gateway tries waking for them (a phone without a passcode then unlocks).
TAKEOVER_TOLERATES = frozenset({"locked"})


def require_usable(device: Device, *, takeover: bool = False) -> None:
    """Raises unless the device is usable: its screen is asked for (the agent's screenshot,
    a person watching the task or taking the phone over) only then. An offline phone
    (``device_offline``) or one that reports a problem (``device_not_ready``, e.g. not
    responding) would send no picture: asking it would only wait for nothing. A person
    taking it over may find it locked: the takeover wakes it, and says so if it cannot."""
    if not device.online:
        raise Rejected("device_offline")
    problems = [p for p in device.problems if not (takeover and p in TAKEOVER_TOLERATES)]
    if problems:
        raise Rejected("device_not_ready", f"{device.name}: {', '.join(problems)}")


def device_wait(devices: Sequence[Device]) -> str | None:
    """What a run waits for when it holds devices and none of them can be used: its wait
    detail, naming them. None when it can go on: without devices, or with the usable ones
    (its tools tell the agent which of the others it cannot use now)."""
    if not devices or any(usable(d) for d in devices):
        return None
    return f"waiting for {', '.join(d.name for d in devices)} to reconnect"


def device_wait_ran_out(run: Run, waiting: str) -> bool:
    """Whether a run, claimed while none of its devices can be used, has waited for these
    same devices for DEVICE_WAIT. A device that comes back wakes the run with its wait
    cleared (``runs.wake_for_devices``), so a run still marked as waiting for them was
    claimed at the wake-up time set DEVICE_WAIT ahead when it began to wait."""
    return run.wait_reason == WaitReason.DEVICE and run.wait_detail == waiting


def device_wait_failed(devices: Sequence[Device]) -> str:
    """The run's summary when it gave up waiting for its devices."""
    why = {p.replace("_", " ") for d in devices for p in d.problems}
    if any(not d.online for d in devices):
        why.add("offline")
    minutes = int(DEVICE_WAIT.total_seconds() // 60)
    return (
        f"{', '.join(d.name for d in devices)} could not be used for {minutes} minutes "
        f"({', '.join(sorted(why))}); reconnect it and send a message to go on."
    )
