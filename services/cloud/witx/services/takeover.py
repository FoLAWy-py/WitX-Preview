"""A person taking over a device .

A task's device can be taken over once the task is paused: the pause put a barrier in
place, so no input of the agent is in flight or can start. A device no task holds can
be taken over directly from the device pool; until the takeover ends no task gets it.
Either way only a usable device (online, no problem reported) can be taken over.
Starting gives the device a new epoch, under which only the person's input runs;
ending it (by the person, after idle time, when their connection drops, when the phone
sends no picture, or by stopping the task) gives the device another epoch, so late
input of the takeover is refused. A task's agent is told to look again before it acts,
and the task stays paused unless the person chose to resume it; a device outside a task
is free again.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.domain.model import Lease, Task, TaskStatus
from witx.domain.rules import Rejected
from witx.services import lifecycle
from witx.store import devices, tasks


async def start(
    conn: AsyncConnection, *, task_id: UUID, device_id: UUID, user_id: UUID, now: datetime
) -> Lease:
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    if task.status != TaskStatus.PAUSED:
        raise Rejected("task_not_paused", "pause the task and wait until it is paused")
    device = await devices.get(conn, device_id, lock=True)
    if device is None:
        raise Rejected("unknown_device")
    lease = await devices.lease(conn, device_id)
    if lease is None or lease.task_id != task.id or lease.run_id is None:
        raise Rejected("device_not_in_task")
    # A phone that does not answer would show nothing but "connecting" until the takeover
    # gave up on it; the person learns why at once instead. A locked one is tried.
    rules.require_usable(device, takeover=True)
    if any(rules.takeover_active(other, now) for other in await devices.task_leases(conn, task.id)):
        raise Rejected("takeover_active", "a device of this task is already taken over")
    await devices.grant_lease(
        conn, tenant_id=task.tenant_id, device_id=device_id, task_id=task.id, run_id=lease.run_id
    )
    takeover_id = uuid4()
    await devices.set_takeover(
        conn,
        device_id,
        takeover_id=takeover_id,
        user_id=user_id,
        expires_at=now + rules.TAKEOVER_IDLE,
    )
    await tasks.emit(conn, task, "takeover.started", device_id=device_id, takeover_id=takeover_id)
    taken = await devices.lease(conn, device_id)
    assert taken is not None
    return taken


async def start_on_device(
    conn: AsyncConnection, *, device_id: UUID, user_id: UUID, now: datetime
) -> Lease:
    """Takes over a device no task holds, from the device pool."""
    device = await devices.get(conn, device_id, lock=True)
    if device is None:
        raise Rejected("unknown_device")
    rules.admit_device_takeover(device, await devices.lease(conn, device_id), now)
    lease = await devices.hold_for_person(
        conn,
        tenant_id=device.tenant_id,
        device_id=device_id,
        takeover_id=uuid4(),
        user_id=user_id,
        expires_at=now + rules.TAKEOVER_IDLE,
    )
    await devices.record_change(conn, device_id, user_id, "taken_over", device.name)
    return lease


async def extend(conn: AsyncConnection, takeover_id: UUID, now: datetime) -> None:
    await devices.extend_takeover(conn, takeover_id, now + rules.TAKEOVER_IDLE)


async def end(
    conn: AsyncConnection,
    *,
    takeover_id: UUID,
    reason: str,
    inputs: int,
    resume: bool = False,
) -> Task | None:
    """Ends a takeover (once; later calls do nothing). Returns the task, None for a
    device taken over outside a task."""
    lease = await devices.lease_by_takeover(conn, takeover_id, lock=True)
    if lease is None:
        return None
    if lease.task_id is None:
        await devices.get(conn, lease.device_id, lock=True)
        await devices.bump_epoch(conn, lease.device_id)  # late input of the takeover is refused
        await devices.release_lease(conn, lease.device_id)
        return None
    task = await tasks.get(conn, lease.task_id, lock=True)
    assert task is not None and lease.takeover_user is not None and lease.run_id is not None
    await devices.get(conn, lease.device_id, lock=True)
    await devices.grant_lease(  # a new epoch: late input of the takeover is refused
        conn,
        tenant_id=task.tenant_id,
        device_id=lease.device_id,
        task_id=task.id,
        run_id=lease.run_id,
    )
    await tasks.emit(
        conn,
        task,
        "takeover.ended",
        device_id=lease.device_id,
        takeover_id=takeover_id,
        reason=reason,
        inputs=inputs,
    )
    await lifecycle.add_input(
        conn,
        task_id=task.id,
        user_id=lease.takeover_user,
        text=f"I controlled device {lease.device_id} myself ({inputs} inputs). The screen "
        "may have changed: take a new screenshot before you act.",
        run_id=lease.run_id,  # the agent that holds the device
        system=True,
    )
    if resume and task.status == TaskStatus.PAUSED:
        await lifecycle.resume(conn, task.id)
    return await tasks.get(conn, task.id)
