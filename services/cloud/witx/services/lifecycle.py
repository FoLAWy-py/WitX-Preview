"""Task and run lifecycle: create, add input, pause, resume, stop, finish, and device leases.

All functions run inside the caller's transaction.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.config import CatalogModel, Limits
from witx.domain import rules
from witx.domain.model import (
    TERMINAL_RUN,
    Lease,
    PermissionMode,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    WaitReason,
)
from witx.domain.rules import Rejected
from witx.services import execution as exec_service
from witx.store import devices, identity, runs, side_turns, tasks

PAUSE_STATES = (TaskStatus.PAUSING, TaskStatus.PAUSED)


async def create_task(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    user_id: UUID,
    request: str,
    constraints: list[str],
    device_ids: list[UUID],
    model: str,
    permission_mode: PermissionMode = PermissionMode.AUTO,
    reasoning_effort: str = "medium",
    timezone: str = "UTC",
    group_id: UUID | None = None,
) -> Task:
    await check_mode_allowed(conn, tenant_id, permission_mode)
    if group_id is not None and await tasks.get_group(conn, group_id) is None:
        raise Rejected("not_found", "group not found")
    rules.check_timezone(timezone)
    # "/plan …" as the first message asks for a plan before any action.
    plan = rules.plan_command(request)
    if plan is not None:
        if plan.kind != "start":
            raise Rejected("invalid_arguments", "start a task with /plan and what to do")
        request = plan.text
    # "/skill …" asks for a skill to be drafted: the agent looks, as in plan mode,
    # and changes nothing. The request keeps the command, so every run reads it as such.
    if rules.skill_command(request) == "":
        raise Rejected("invalid_arguments", "write what the skill is for after /skill")
    plan = plan or (rules.PlanCommand("start") if rules.skill_command(request) else None)
    # A plain request has no goal; "/goal …" as the first message makes one.
    command = rules.goal_command(request)
    goal = command.goal if command and command.kind == "set" else ""
    request = goal or request
    for device_id in device_ids:
        device = await devices.get(conn, device_id)
        if device is None:
            raise Rejected("unknown_device", str(device_id))
        if not device.enabled:
            raise Rejected("device_not_enabled", f"{device.name}: an owner enables it first")
        if device.problems:
            raise Rejected("device_not_ready", f"{device.name}: {', '.join(device.problems)}")
    task = await tasks.insert(
        conn,
        id=uuid4(),
        tenant_id=tenant_id,
        created_by=user_id,
        goal=goal,
        constraints=constraints,
        model=model,
        device_ids=device_ids,
        permission_mode=permission_mode,
        reasoning_effort=reasoning_effort,
        timezone=timezone,
        request=request,
        plan_mode=plan is not None,
        group_id=group_id,
    )
    await tasks.add_input(
        conn,
        tenant_id=tenant_id,
        task_id=task.id,
        kind="goal",
        text=request,
        revision=1,
        created_by=user_id,
    )
    for text in constraints:
        await tasks.add_input(
            conn,
            tenant_id=tenant_id,
            task_id=task.id,
            kind="constraint",
            text=text,
            revision=1,
            created_by=user_id,
        )
    run = await runs.insert(
        conn, id=uuid4(), tenant_id=tenant_id, task_id=task.id, goal=request, device_ids=device_ids
    )
    assert run is not None
    await tasks.emit(
        conn, task, "task.created", goal=request, run_id=run.id, permission_mode=permission_mode
    )
    if plan is not None:
        await tasks.emit(conn, task, "task.plan_mode", on=True, by="user")
    return task


async def check_mode_allowed(conn: AsyncConnection, tenant_id: UUID, mode: PermissionMode) -> None:
    if mode == PermissionMode.FULL and not await identity.allows_full_access(conn, tenant_id):
        raise Rejected("full_access_not_allowed", "a tenant owner has not allowed full access")


async def set_permission_mode(
    conn: AsyncConnection, *, task_id: UUID, mode: PermissionMode, limits: Limits, now: datetime
) -> Task:
    """Changes when the task stops to ask. Takes effect immediately for unsent input."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    await check_mode_allowed(conn, task.tenant_id, mode)
    if mode == task.permission_mode:
        return task
    await tasks.update(conn, task.id, permission_mode=mode.value)
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    await exec_service.reapply_mode(conn, updated, mode, limits=limits, now=now)
    await tasks.emit(conn, task, "task.permission_mode", permission_mode=mode)
    return updated


async def set_model(
    conn: AsyncConnection,
    *,
    task_id: UUID,
    model: str,
    catalog: Mapping[str, CatalogModel],
    fallback_for: str | None = None,
) -> Task:
    """Switches the model the task's agent uses, from its next step on (the history is
    kept). A run that stopped on a model error is woken to try the new model.
    ``fallback_for`` is the error the task fell back for, when the switch was automatic.
    An effort below the new model's lowest goes up to it."""
    rules.check_model(model, catalog)
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    if model == task.model:
        return task
    effort = rules.effort_on_model(task.reasoning_effort, catalog[model].lowest_effort)
    await tasks.update(conn, task.id, model=model, reasoning_effort=effort)
    if effort != task.reasoning_effort:
        await tasks.emit(
            conn, task, "task.reasoning_effort", effort=effort, previous=task.reasoning_effort
        )
    await runs.wake_after_model_error(conn, task.id)
    await tasks.emit(
        conn,
        task,
        "task.model",
        model=model,
        previous=task.model,
        **({"fallback_for": fallback_for} if fallback_for else {}),
    )
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


# A device leaves a task only while nothing can be under way on it.
RUNNING_STATES = (TaskStatus.ACTIVE, TaskStatus.PAUSING, TaskStatus.STOPPING)


async def set_devices(
    conn: AsyncConnection, *, task_id: UUID, user_id: UUID, device_ids: list[UUID]
) -> Task:
    """Changes the phones a task uses. A phone can join at any time; the agent's next
    step takes it. A phone leaves only while the task is not running (paused or
    finished), so no input can be under way on it. The agent is told in a note."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    wanted = list(dict.fromkeys(device_ids))
    added = [d for d in wanted if d not in task.device_ids]
    removed = [d for d in task.device_ids if d not in wanted]
    if not added and not removed:
        return task
    if removed and task.status in RUNNING_STATES:
        raise Rejected("pause_first", "pause the task before taking a phone away from it")
    names: dict[UUID, str] = {}
    for device_id in added + removed:
        device = await devices.get(conn, device_id)
        if device is None:
            raise Rejected("unknown_device", str(device_id))
        if device_id in added:
            if not device.enabled:
                raise Rejected("device_not_enabled", f"{device.name}: an owner enables it first")
            if device.problems:
                raise Rejected("device_not_ready", f"{device.name}: {', '.join(device.problems)}")
        names[device_id] = device.name
    await tasks.update(conn, task.id, device_ids=wanted)
    for run in await runs.for_task(conn, task.id):
        if run.parent_run_id is None and run.status not in TERMINAL_RUN:
            await runs.set_device_ids(conn, run.id, wanted)
            await runs.wake(conn, run.id, [WaitReason.DEVICE])
    for device_id in removed:
        lease = await devices.lease(conn, device_id, lock=True)
        if lease is not None and lease.task_id == task.id and lease.takeover_id is None:
            await devices.release_lease(conn, device_id)
            await tasks.emit(conn, task, "device.released", device_id=device_id)
    # The line people see, then the note the agent reads (the very next event).
    await tasks.emit(
        conn,
        task,
        "task.devices",
        added=[names[d] for d in added],
        removed=[names[d] for d in removed],
    )
    lines = [f"- {d}: {names[d]}" for d in added]
    text = "The user changed the phones of this task."
    if added:
        text += " Added (take a screenshot before acting on them):\n" + "\n".join(lines)
    if removed:
        text += "\nRemoved, do not use: " + ", ".join(f"{names[d]} ({d})" for d in removed)
    # A finished task does not start over for new phones: they wait for the next message.
    await add_input(
        conn, task_id=task.id, user_id=user_id, text=text, resume_finished=False, system=True
    )
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def agent_set_goal(conn: AsyncConnection, task: Task, goal: str) -> None:
    """The agent gives a complex or long task a goal to track (only once: never over a
    goal the user set, and not again after the user cleared one)."""
    rules.check_agent_goal(task.goal, await tasks.goal_ever_set(conn, task.id))
    await tasks.update(conn, task.id, goal=goal)
    await tasks.emit(conn, task, "task.goal", goal=goal, previous="", by="agent")


async def end_goal(
    conn: AsyncConnection, task: Task, outcome: Literal["achieved", "impossible"], reason: str
) -> None:
    """The goal has been judged met, or can never be met: it is over and the task has none
    any more (the user may set another). Kept in the task's events with how long it took."""
    turns, since = await tasks.goal_progress(conn, task.id)
    await tasks.emit(
        conn,
        task,
        f"goal.{outcome}",
        goal=task.goal,
        reason=reason,
        turns=turns,
        seconds=int((datetime.now(UTC) - since).total_seconds()),
    )
    await tasks.update(conn, task.id, goal="", goal_paused=False)
    await tasks.emit(conn, task, "task.goal", goal="", previous=task.goal, by="evaluator")


async def set_reasoning_effort(
    conn: AsyncConnection, *, task_id: UUID, effort: str, catalog: Mapping[str, CatalogModel]
) -> Task:
    """How hard the agent thinks, from its next step on, as far down as its model
    goes."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    model = catalog.get(task.model)
    rules.check_reasoning_effort(effort, model.lowest_effort if model else rules.LOWEST_EFFORT)
    if effort == task.reasoning_effort:
        return task
    await tasks.update(conn, task.id, reasoning_effort=effort)
    await tasks.emit(
        conn, task, "task.reasoning_effort", effort=effort, previous=task.reasoning_effort
    )
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def add_input(
    conn: AsyncConnection,
    *,
    task_id: UUID,
    user_id: UUID,
    text: str,
    resume_finished: bool = True,
    run_id: UUID | None = None,
    system: bool = False,
) -> Task:
    """Adds a message from the user to the conversation; the main agent reads it on its
    next step. ``/goal …`` also sets the task's goal (the title stays). A message to a
    finished or stopped task resumes it with a new root run, unless it only records a
    change (``resume_finished`` false: it waits for the person's next message); on a
    paused task it waits for resume.

    ``run_id`` names a sub-agent: the message goes to it alone and wakes only it, as an
    answer to its question; commands are not read in it. Once that sub-agent has
    finished, the message goes to the main agent instead. Sub-agents read nothing
    else from the user; while the main agent waits for them, a message to it starts its
    side turn, in which it can pass the word on."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    if task.status == TaskStatus.STOPPING:
        raise Rejected("task_stopping", "wait until the stop completes")
    target = await _sub_agent(conn, task, run_id)
    kind = "system" if system else "note"
    if target is not None:
        seq = await tasks.add_input(
            conn,
            tenant_id=task.tenant_id,
            task_id=task.id,
            kind=kind,
            text=text,
            revision=task.revision,
            created_by=user_id,
            run_id=target.id,
        )
        await tasks.emit(conn, task, "input.added", seq=seq, kind=kind, run_id=target.id)
        if task.status == TaskStatus.ACTIVE:
            await runs.wake(conn, target.id, (WaitReason.USER_INPUT, WaitReason.MODEL_RETRY))
        updated = await tasks.get(conn, task.id)
        assert updated is not None
        return updated
    command = None if system else rules.goal_command(text)
    if command and command.kind in ("pause", "resume") and not task.goal:
        raise Rejected("no_goal", "the task has no goal; set one with /goal")
    plan = None if system else rules.plan_command(text)
    if plan is not None and plan.kind != "start":
        if not task.plan_mode:
            raise Rejected("not_planning", "the task is not in plan mode")
        if plan.kind == "approve" and not await tasks.plan_proposed(conn, task.id):
            raise Rejected(
                "no_plan",
                "the agent has not presented a plan yet; /plan off leaves plan mode without one",
            )
    seq = await tasks.add_input(
        conn,
        tenant_id=task.tenant_id,
        task_id=task.id,
        kind=kind,
        text=text,
        revision=task.revision,
        created_by=user_id,
    )
    await tasks.emit(conn, task, "input.added", seq=seq, kind=kind)
    goal = await _apply_goal(conn, task, command, user_id) if command else None
    if plan is not None:
        await _apply_plan(conn, task, plan)
        goal = goal or (plan.text or None)
    if task.status == TaskStatus.ACTIVE:
        for run in await runs.for_task(conn, task.id):
            if run.parent_run_id is not None:
                # A sub-agent does not read it, but one that waits to retry the model may.
                await runs.wake(conn, run.id, (WaitReason.MODEL_RETRY,))
            elif run.status not in TERMINAL_RUN and await runs.delegating(conn, run.id):
                await side_turns.insert(
                    conn,
                    id=uuid4(),
                    tenant_id=task.tenant_id,
                    task_id=task.id,
                    run_id=run.id,
                    input_seq=seq,
                    text=text,
                )
            else:
                await runs.wake(conn, run.id, (WaitReason.USER_INPUT, WaitReason.MODEL_RETRY))
    elif (
        task.status in PAUSE_STATES
        or not resume_finished
        # Pausing or clearing a goal asks for less work, not more.
        or (command is not None and command.kind in ("pause", "clear"))
    ):
        pass  # kept for the agent; resuming the task, or the next message, wakes it
    else:
        await _resume(conn, task, goal or task.goal or task.request)
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def withdraw_input(conn: AsyncConnection, *, task_id: UUID, seq: int, user_id: UUID) -> Task:
    """Withdraws the person's own message while it still waits for the main agent.
    The row goes first, so a step being started now either read it already (then the
    withdrawal is refused) or never will."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    row = next((i for i in await tasks.inputs(conn, task.id) if i["seq"] == seq), None)
    if row is None or row["created_by"] != user_id:
        raise Rejected("not_found", "no such message of yours")
    if rules.goal_command(row["text"]) or rules.plan_command(row["text"]):
        raise Rejected("already_read", "a command takes effect when it is sent")
    await tasks.delete_input(conn, task.id, seq)
    queued = rules.queued_input(
        kind=row["kind"],
        run_id=row["run_id"],
        seq=seq,
        read_through=await runs.read_through(conn, task.id),
        seen=await side_turns.seen_inputs(conn, task.id),
        live=task.status in (TaskStatus.ACTIVE, *PAUSE_STATES),
    )
    if not queued:
        raise Rejected("already_read", "the agent has read this message")
    await side_turns.withdraw(conn, task.id, seq, datetime.now(UTC))
    await tasks.emit(conn, task, "input.withdrawn", seq=seq)
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def _queued(conn: AsyncConnection, task: Task) -> list[int]:
    """The person's messages the main agent has not read yet."""
    read_through = await runs.read_through(conn, task.id)
    seen = await side_turns.seen_inputs(conn, task.id)
    return [
        int(i["seq"])
        for i in await tasks.inputs(conn, task.id)
        if rules.queued_input(
            kind=i["kind"],
            run_id=i["run_id"],
            seq=int(i["seq"]),
            read_through=read_through,
            seen=seen,
            live=task.status in (TaskStatus.ACTIVE, *PAUSE_STATES),
        )
    ]


async def steer(conn: AsyncConnection, *, task_id: UUID) -> Task:
    """The main agent reads the queued messages now: its inputs not sent to a
    phone yet are cancelled, and a model call under way is dropped (its answer is not
    carried out: the run is no longer running when it comes back). An input already on
    the phone finishes first. Nothing happens when no message is queued, so a second
    press does not drop the step that is already reading it; a run waiting for its
    sub-agents (the messages went to a side turn, D57), paused, or stopped for the
    person to decide is left as it is."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    if task.status != TaskStatus.ACTIVE:
        raise Rejected("task_not_active", "resume the task first")
    roots = []
    for listed in await runs.for_task(conn, task.id):
        if listed.parent_run_id is None and listed.status not in TERMINAL_RUN:
            run = await runs.get(conn, listed.id, lock=True)  # a step under way commits first
            if run is not None:
                roots.append(run)
    if not await _queued(conn, task):  # counted after the locks: a step just begun read it
        return task
    for run in roots:
        if run.status == RunStatus.WAITING and run.wait_reason in (
            WaitReason.CHILDREN,
            WaitReason.PAUSED,
            WaitReason.USER_INPUT,  # the message already woke it, or it waits for you
        ):
            continue
        if run.status not in (RunStatus.RUNNING, RunStatus.WAITING):
            continue
        await exec_service.cancel_unsent(conn, task.id, "steered", run.id)
        await runs.set_state(conn, run.id, RunStatus.RUNNABLE)
    await tasks.emit(conn, task, "task.steered")
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def read_the_rest(conn: AsyncConnection, task_id: UUID, read_through: int) -> None:
    """After the main agent finished: a message it did not read yet starts the next
    round, as if sent to the finished task."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None or task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        return
    unread = [
        i
        for i in await tasks.inputs(conn, task.id)
        if i["kind"] == "note" and i["run_id"] is None and int(i["seq"]) > read_through
    ]
    if unread:
        await _resume(conn, task, task.goal or task.request)


async def _sub_agent(conn: AsyncConnection, task: Task, run_id: UUID | None) -> Run | None:
    """The sub-agent a message goes to; None for the main agent (the conversation), also
    in place of a sub-agent that has finished meanwhile."""
    if run_id is None:
        return None
    run = await runs.get(conn, run_id)
    if run is None or run.task_id != task.id:
        raise Rejected("not_found", "no such agent in this task")
    if run.parent_run_id is None or run.status in TERMINAL_RUN:
        return None
    return run


async def _apply_goal(
    conn: AsyncConnection, task: Task, command: rules.GoalCommand, user_id: UUID
) -> str | None:
    """Sets, clears, pauses or resumes the goal; returns a new goal (or "" when cleared).
    A new or cleared goal ends the task's grants: they were given for the old one."""
    if command.kind == "pause" and not task.goal_paused:
        await tasks.update(conn, task.id, goal_paused=True)
        await tasks.emit(conn, task, "goal.paused", goal=task.goal)
    elif command.kind == "resume" and task.goal_paused:
        await tasks.update(conn, task.id, goal_paused=False)
        await tasks.emit(conn, task, "goal.resumed", goal=task.goal)
    elif command.kind in ("set", "clear"):
        goal = command.goal if command.kind == "set" else ""
        if goal != task.goal or task.goal_paused:
            await tasks.update(conn, task.id, goal=goal, goal_paused=False)
            await tasks.emit(conn, task, "task.goal", goal=goal, previous=task.goal)
        if goal != task.goal:
            await exec_service.revoke_grants(conn, task, user_id=user_id)
        return goal
    return None


async def _apply_plan(conn: AsyncConnection, task: Task, command: rules.PlanCommand) -> None:
    """Turns plan mode on (``/plan …``), or off: with the user's approval of the plan the
    agent presented, or without one. Only a message from the user gets here."""
    if command.kind == "start" and not task.plan_mode:
        await tasks.update(conn, task.id, plan_mode=True)
        await tasks.emit(conn, task, "task.plan_mode", on=True, by="user")
    elif command.kind in ("approve", "off") and task.plan_mode:
        await tasks.update(conn, task.id, plan_mode=False)
        await tasks.emit(conn, task, "task.plan_mode", on=False, approved=command.kind == "approve")


async def _resume(conn: AsyncConnection, task: Task, goal: str) -> None:
    await tasks.update(conn, task.id, status=TaskStatus.ACTIVE.value, result=None)
    run = await runs.insert(
        conn,
        id=uuid4(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        goal=goal,
        device_ids=task.device_ids,
    )
    assert run is not None
    await tasks.emit(conn, task, "task.status", status="active", run_id=run.id)


async def stop(conn: AsyncConnection, task_id: UUID) -> Task:
    """Requests a stop, also of a paused task. The task is only `stopped` once every
    device confirmed its barrier; until then it is `stopping` and actions already sent
    may still land. Stopping releases the devices."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    if task.status not in (TaskStatus.ACTIVE, *PAUSE_STATES):
        return task
    await tasks.update(
        conn, task.id, status=TaskStatus.STOPPING.value, control_revision=task.control_revision + 1
    )
    await exec_service.cancel_unsent(conn, task.id, "task_stopped")
    for run in await runs.for_task(conn, task.id):
        if run.status not in TERMINAL_RUN:
            await runs.set_state(conn, run.id, RunStatus.CANCELLED)
    leases = await devices.task_leases(conn, task.id)
    for lease in leases:
        await devices.request_stop(conn, lease.device_id)
    status = TaskStatus.STOPPING if leases else TaskStatus.STOPPED
    if not leases:
        await tasks.update(conn, task.id, status=status.value)
    await tasks.emit(conn, task, "task.status", status=status.value)
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def pause(conn: AsyncConnection, task_id: UUID) -> Task:
    """Requests a pause: unsent actions are cancelled, the runs wait without calling the
    model, and every device gets a stop barrier but stays with the task. The task is
    only `paused` once every device confirmed its barrier."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    if task.status != TaskStatus.ACTIVE:
        return task
    await tasks.update(
        conn, task.id, status=TaskStatus.PAUSING.value, control_revision=task.control_revision + 1
    )
    await exec_service.cancel_unsent(conn, task.id, "task_paused")
    for run in await runs.for_task(conn, task.id):
        if run.status not in TERMINAL_RUN:
            # Without a wake-up time nothing claims or wakes the run until resume.
            await runs.set_state(conn, run.id, RunStatus.WAITING, wait_reason=WaitReason.PAUSED)
    leases = await devices.task_leases(conn, task.id)
    for lease in leases:
        await devices.request_stop(conn, lease.device_id)
    status = TaskStatus.PAUSING if leases else TaskStatus.PAUSED
    if not leases:
        await tasks.update(conn, task.id, status=status.value)
    await tasks.emit(conn, task, "task.status", status=status.value)
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def resume(conn: AsyncConnection, task_id: UUID) -> Task:
    """Resumes a paused task. Each of its devices gets a new epoch: input of the paused
    epoch stays refused, and screenshots taken before are no longer current, so the
    agent observes again before any input."""
    task = await tasks.get(conn, task_id, lock=True)
    if task is None:
        raise Rejected("not_found")
    if task.status == TaskStatus.PAUSING:
        raise Rejected("task_pausing", "wait until every device confirmed the pause")
    if task.status != TaskStatus.PAUSED:
        return task
    leases = await devices.task_leases(conn, task.id)
    if any(rules.takeover_active(lease, datetime.now(UTC)) for lease in leases):
        raise Rejected("takeover_active", "end the takeover first")
    for lease in leases:
        assert lease.run_id is not None  # a task's lease always has its writer run
        await devices.get(conn, lease.device_id, lock=True)
        await devices.grant_lease(
            conn,
            tenant_id=task.tenant_id,
            device_id=lease.device_id,
            task_id=task.id,
            run_id=lease.run_id,
        )
    await tasks.update(
        conn, task.id, status=TaskStatus.ACTIVE.value, control_revision=task.control_revision + 1
    )
    for run in await runs.for_task(conn, task.id):
        await runs.wake(conn, run.id, (WaitReason.PAUSED,))
    await tasks.emit(conn, task, "task.status", status="active")
    updated = await tasks.get(conn, task.id)
    assert updated is not None
    return updated


async def stop_acknowledged(conn: AsyncConnection, *, device_id: UUID, stop_id: UUID) -> None:
    lease = await devices.lease(conn, device_id, lock=True)
    if lease is None or lease.stop_id != stop_id or lease.task_id is None:
        return  # duplicate or outdated acknowledgement (only a task's lease has a barrier)
    task = await tasks.get(conn, lease.task_id, lock=True)
    if task is not None and task.status == TaskStatus.PAUSING:
        # A pause: the barrier holds and the device stays with the task.
        await devices.clear_stop(conn, device_id)
        await tasks.emit(conn, task, "device.paused", device_id=device_id)
        if not any(other.stop_id for other in await devices.task_leases(conn, task.id)):
            await tasks.update(conn, task.id, status=TaskStatus.PAUSED.value)
            await tasks.emit(conn, task, "task.status", status="paused")
        return
    await devices.release_lease(conn, device_id)
    await tasks.emit(conn, lease.task_id, "device.released", device_id=device_id)
    if not await devices.task_leases(conn, lease.task_id):
        task = await tasks.get(conn, lease.task_id, lock=True)
        if task is not None and task.status == TaskStatus.STOPPING:
            await tasks.update(conn, task.id, status=TaskStatus.STOPPED.value)
            await tasks.emit(conn, task, "task.status", status="stopped")


async def finish_run(
    conn: AsyncConnection,
    run: Run,
    status: RunStatus,
    result: dict[str, Any],
    *,
    epoch: int | None = None,
) -> bool:
    """Ends a run. A child hands its devices and result back to its parent; the root
    run's outcome becomes the task's outcome."""
    if not await runs.set_state(conn, run.id, status, epoch=epoch, result=result):
        return False
    await tasks.emit(conn, run.task_id, "run.status", run_id=run.id, status=status.value)
    if run.parent_run_id is not None:
        for lease in await devices.leases(conn, run.device_ids):
            if lease.run_id == run.id:
                await devices.grant_lease(
                    conn,
                    tenant_id=run.tenant_id,
                    device_id=lease.device_id,
                    task_id=run.task_id,
                    run_id=run.parent_run_id,
                )
        if run.spawn_key is not None:
            await _resolve_delegate(conn, run.parent_run_id, run.spawn_key.rsplit(":", 1)[0])
        return True
    task = await tasks.get(conn, run.task_id, lock=True)
    assert task is not None
    if task.status == TaskStatus.ACTIVE:
        task_status = TaskStatus.COMPLETED if status == RunStatus.COMPLETED else TaskStatus.FAILED
        await tasks.update(conn, task.id, status=task_status.value, result=result)
        for lease in await devices.task_leases(conn, task.id):
            await devices.release_lease(conn, lease.device_id)
        await tasks.emit(conn, task, "task.status", status=task_status.value)
    return True


async def _resolve_delegate(conn: AsyncConnection, parent_run_id: UUID, call_id: str) -> None:
    """A delegate call is answered once every sub-agent it started has finished: one
    result per subtask, in the order given, with the device it worked on."""
    siblings = [
        c
        for c in await runs.children(conn, parent_run_id)
        if c.spawn_key is not None and c.spawn_key.rsplit(":", 1)[0] == call_id
    ]
    if not siblings or any(c.status not in TERMINAL_RUN for c in siblings):
        return
    call = await runs.call_by_call_id(conn, parent_run_id, call_id)
    if call is None:
        return
    siblings.sort(key=lambda c: int(str(c.spawn_key).rsplit(":", 1)[1]))
    results = [
        {
            "goal": c.goal,
            "device_id": str(c.device_ids[0]) if c.device_ids else None,
            "status": c.status.value,
            **(c.result or {}),
        }
        for c in siblings
    ]
    await exec_service.resolve(conn, call, {"status": "done", "results": results})


async def ensure_leases(conn: AsyncConnection, task: Task, run: Run) -> dict[UUID, Lease] | None:
    """Gives the run input control of its devices. Returns None while another task or a
    person holds one of them. A device lent to a child run is skipped (the parent cannot
    use it)."""
    held: dict[UUID, Lease] = {}
    for device_id in run.device_ids:
        if await devices.get(conn, device_id, lock=True) is None:
            continue
        lease = await devices.lease(conn, device_id)
        if rules.held_by_nobody(lease, datetime.now(UTC)):
            lease = await devices.grant_lease(
                conn, tenant_id=task.tenant_id, device_id=device_id, task_id=task.id, run_id=run.id
            )
            await tasks.emit(conn, task, "device.leased", device_id=device_id, run_id=run.id)
        if lease is None or lease.task_id != task.id:
            return None
        if lease.run_id == run.id:
            held[device_id] = lease
    return held


async def lend_device(conn: AsyncConnection, *, task: Task, child: Run, device_id: UUID) -> Lease:
    return await devices.grant_lease(
        conn, tenant_id=task.tenant_id, device_id=device_id, task_id=task.id, run_id=child.id
    )
