"""Before the model call: settle what the run waits on, take its first screenshots,
build the step's context, and summarize its history when due."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection
from witx_contracts import logs

from witx import telemetry
from witx.domain import rules
from witx.domain.model import (
    Action,
    ActionStatus,
    Effect,
    Lease,
    Observation,
    ObservationStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    ToolCall,
    WaitReason,
)
from witx.harness import context, relay, tools
from witx.harness.turn import Prepared, utcnow
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.store import devices, execution, identity, runs, tasks
from witx.store import memories as memory_store

if TYPE_CHECKING:
    from witx.harness.loop import Harness

RECHECK = timedelta(seconds=30)
DEVICE_RECHECK = timedelta(seconds=15)


async def prepare(harness: "Harness", run_id: UUID, tenant_id: UUID, epoch: int) -> Prepared | None:
    async with harness.db.tenant(tenant_id) as conn:
        # The task first, then the run: the order a stop or pause takes.
        found = await runs.get(conn, run_id)
        task = await tasks.get(conn, found.task_id, lock=True) if found else None
        run = await runs.get(conn, run_id, lock=True)
        if run is None or run.status != RunStatus.RUNNING or run.epoch != epoch:
            return None
        assert task is not None
        if task.status in lifecycle.PAUSE_STATES:
            await runs.set_state(
                conn, run.id, RunStatus.WAITING, epoch=epoch, wait_reason=WaitReason.PAUSED
            )
            return None
        if task.status != TaskStatus.ACTIVE:
            await runs.set_state(conn, run.id, RunStatus.CANCELLED, epoch=epoch)
            return None
        now = utcnow()
        await exec_service.expire_approvals(conn, run, now)
        await _settle_observations(harness, conn, run, now)
        calls = await runs.calls_for_run(conn, run.id)
        open_calls = [c for c in calls if c.output is None]
        if open_calls:
            reason = await wait_reason(conn, open_calls)
            await runs.set_state(
                conn,
                run.id,
                RunStatus.WAITING,
                epoch=epoch,
                wait_reason=reason,
                next_wake_at=now + RECHECK,
            )
            return None
        steps = await runs.completed_steps(conn, run.id)
        if len(steps) >= harness.limits.max_steps_per_run:
            limit = harness.limits.max_steps_per_run
            await lifecycle.finish_run(
                conn,
                run,
                RunStatus.FAILED,
                {
                    # ``failure`` lets the web word it in the reader's language.
                    "summary": f"The agent reached the step limit ({limit} steps) "
                    "without finishing.",
                    "failure": "step_limit",
                    "completed_at": now.isoformat(),
                },
                epoch=epoch,
            )
            return None
        latest = await runs.latest_steps(conn, run.id, rules.UNFINISHED_STEPS_MAX)
        if rules.steps_keep_failing([status for _, status in latest]):
            # Each try is a paid model call: the user resumes the run (once more).
            await runs.finish_step(conn, latest[0][0], status="abandoned")
            await runs.set_state(
                conn,
                run.id,
                RunStatus.WAITING,
                epoch=epoch,
                wait_reason=WaitReason.USER_INPUT,
                wait_detail="model error: the step did not finish "
                f"{rules.UNFINISHED_STEPS_MAX} times in a row",
            )
            await tasks.emit(
                conn, task, "run.model_error", run_id=run.id, code="step_failed", retrying=False
            )
            return None
        held = await lifecycle.ensure_leases(conn, task, run)
        if held is None:
            await runs.set_state(
                conn,
                run.id,
                RunStatus.WAITING,
                epoch=epoch,
                wait_reason=WaitReason.DEVICE,
                wait_detail="a device is in use by another task",
                next_wake_at=now + DEVICE_RECHECK,
            )
            return None
        if await _wait_for_devices(conn, run, held, now):
            return None
        first_screens: list[Observation] = []
        if not steps:
            started = await _first_screens(harness, conn, task, run, now)
            if started is None:
                await runs.set_state(
                    conn,
                    run.id,
                    RunStatus.WAITING,
                    epoch=epoch,
                    wait_reason=WaitReason.OBSERVATION,
                    next_wake_at=now + RECHECK,
                )
                return None
            first_screens = started
        owner = await tasks.creator(conn, task.id)
        kept = await memory_store.for_task(
            conn, task.id, auto=await identity.auto_memory(conn, task.tenant_id, owner)
        )
        # The main agent's read is held against a withdrawal.
        inputs = [
            dict(r) for r in await tasks.inputs(conn, task.id, share=run.parent_run_id is None)
        ]
        ctx = context.RunContext(
            task=task,
            run=run,
            first_screens=first_screens,
            # Preferences and corrections: the main agent's first turn; notes about
            # apps: when a screenshot first shows the app.
            memories=[m for m in kept if m.kind != "app"]
            if not steps and run.parent_run_id is None
            else (),
            app_memories=memory_store.by_app(kept),
            devices=[d for d in await devices.list_all(conn) if d.id in run.device_ids],
            inputs=inputs,
            agents=await relay.agent_names(conn, run) if run.parent_run_id is None else {},
            questions=(
                [dict(q) for q in await tasks.questions(conn, task.id)]
                if run.parent_run_id is None
                else ()
            ),
            steps=steps,
            calls=calls,
            earlier=[
                (_finished_at(r.result, r.created_at), str(r.result["summary"]))
                for r in await runs.for_task(conn, task.id)
                if r.parent_run_id is None
                and r.id != run.id
                and r.created_at < run.created_at
                and r.result
                and r.result.get("summary")
            ],
            done=await _done(conn, task, run, inputs) if not steps else (),
        )
        new, input_seq = context.new_items(ctx)
        step_id = uuid4()
        await runs.start_step(
            conn,
            id=step_id,
            tenant_id=tenant_id,
            run_id=run.id,
            model=task.model,
            input_items=new,
            input_seq=input_seq,
            request={
                "model": task.model,
                "tools": [t["name"] for t in tools.TOOLS],
                "history_steps": len(steps),
                # The plugins whose skills this step could load (spec §8.3, D85).
                "plugins": {
                    s.plugin: {"version": s.version, "sha256": s.digest}
                    for s in harness.skills().values()
                },
            },
        )
        read = [
            i
            for i in ctx.inputs
            if i["kind"] in ("note", "system")
            and i["run_id"] is None
            and int(i["seq"]) > (steps[-1].input_seq if steps else 0)
        ]
        if run.parent_run_id is None and read:
            # The person's queued messages move into the conversation.
            await tasks.emit(conn, task, "input.read", through=input_seq)
        logs.bind(task_id=task.id, step_id=step_id)
        chosen = await identity.allowed_providers(conn, task.tenant_id)
        attempts = await runs.latest_summaries(conn, run.id)
        summary = next((s for s in reversed(attempts) if s.summary), None)
        unknown = [
            f"{a.kind} on {a.target!r} (device {a.device_id}, action {a.id})"
            for a in await execution.task_actions(conn, task.id)
            if a.run_id == run.id and a.effect == Effect.UNKNOWN
        ]
        return Prepared(
            run,
            task,
            epoch,
            step_id,
            context.history(steps, summary, unknown) + new,
            None if chosen is None else tuple(chosen),
            steps=list(steps),
            new=new,
            summary=summary,
            last_attempt=attempts[-1].through_seq if attempts else None,
            last_input_tokens=await runs.last_input_tokens(conn, run.id),
            unknown=unknown,
            input_seq=input_seq,
        )


def _finished_at(result: Mapping[str, object], started: datetime) -> datetime:
    """When a run finished, as its result says; its start for a result without it."""
    at = result.get("completed_at")
    return datetime.fromisoformat(at) if isinstance(at, str) else started


async def compress(harness: "Harness", prepared: Prepared) -> Prepared:
    """Summarizes the run's earlier steps when it is due, outside any
    transaction; the request then carries the summary instead of them. A failed
    summary changes nothing and is tried again later."""
    point = rules.compression_point(
        [s.seq for s in prepared.steps],
        last_attempt=prepared.last_attempt,
        last_input_tokens=prepared.last_input_tokens,
        after_steps=harness.limits.compress_after_steps,
        at_tokens=harness.limits.compress_at_tokens,
        keep_steps=harness.limits.compress_keep_steps,
    )
    if point is None or harness.compressor is None:
        return prepared
    # What the new summary takes in: the history as it stands, up to the input of
    # the step it ends at (that step's own output stays word for word).
    upto = [
        replace(s, output_items=[]) if s.seq == point else s
        for s in prepared.steps
        if s.seq <= point
    ]
    with telemetry.span("agent.compress", task_id=prepared.task.id, through=point):
        made = await harness.compressor.summarize(
            model=prepared.task.model,
            items=context.history(upto, prepared.summary, prepared.unknown),
            allowed_providers=prepared.allowed_providers,
            reasoning_effort=prepared.task.reasoning_effort,
        )
    async with harness.db.tenant(prepared.task.tenant_id) as conn:
        stored = await runs.add_summary(
            conn,
            tenant_id=prepared.task.tenant_id,
            run_id=prepared.run.id,
            through_seq=point,
            summary=made.summary,
            call=made.record,
        )
        await tasks.emit(
            conn,
            prepared.task,
            "run.compressed",
            run_id=prepared.run.id,
            through=point,
            ok=made.summary is not None,
        )
    if made.summary is None:
        return prepared
    items = context.history(prepared.steps, stored, prepared.unknown) + prepared.new
    return replace(prepared, items=items, summary=stored, last_attempt=point)


async def _wait_for_devices(
    conn: AsyncConnection, run: Run, held: Mapping[UUID, Lease], now: datetime
) -> bool:
    """Whether the run waits instead of calling the model: it holds devices and none of
    them can be used. It keeps them, and a Gateway report that one is back wakes
    it (``runs.wake_for_devices``); after ``rules.DEVICE_WAIT`` it fails. The device
    rows are locked (``ensure_leases``), so a report that commits after this read
    finds the run waiting and wakes it."""
    phones = [d for d in [await devices.get(conn, i) for i in held] if d is not None]
    waiting = rules.device_wait(phones)
    if waiting is None:
        return False
    if rules.device_wait_ran_out(run, waiting):
        await lifecycle.finish_run(
            conn,
            run,
            RunStatus.FAILED,
            {"summary": rules.device_wait_failed(phones), "completed_at": now.isoformat()},
            epoch=run.epoch,
        )
        return True
    await runs.set_state(
        conn,
        run.id,
        RunStatus.WAITING,
        epoch=run.epoch,
        wait_reason=WaitReason.DEVICE,
        wait_detail=waiting,
        next_wake_at=now + rules.DEVICE_WAIT,
    )
    # The task page shows why it waits.
    await tasks.emit(conn, run.task_id, "run.status", run_id=run.id, status=RunStatus.WAITING.value)
    return True


async def _first_screens(
    harness: "Harness", conn: AsyncConnection, task: Task, run: Run, now: datetime
) -> list[Observation] | None:
    """A screenshot of each device the run holds, taken as it starts, so its first
    input needs no extra turn; None while one is still on its way. A device
    that is offline or not ready gets none; one that does not arrive in time counts
    as failed and the agent looks again itharness."""
    shots: list[Observation] = []
    waiting = False
    for device_id in run.device_ids:
        device = await devices.get(conn, device_id)
        lease = await devices.lease(conn, device_id)
        if device is None or not rules.usable(device):
            continue
        if lease is None or lease.run_id != run.id:
            continue
        obs = await execution.first_observation(
            conn, task_id=task.id, device_id=device_id, epoch=lease.epoch, since=run.created_at
        )
        if obs is None:
            await exec_service.request_observation(
                conn, task=task, device_id=device_id, epoch=lease.epoch
            )
            waiting = True
        elif obs.status in (ObservationStatus.REQUESTED, ObservationStatus.SENT):
            if now - obs.requested_at <= harness.limits.observation_timeout:
                waiting = True
                continue
            await execution.fail_observation(conn, obs.id, "timeout")
            failed = await execution.get_observation(conn, obs.id)
            assert failed is not None
            shots.append(failed)
        else:
            shots.append(obs)
    return None if waiting else shots


async def _settle_observations(
    harness: "Harness", conn: AsyncConnection, run: Run, now: datetime
) -> None:
    """Fails screenshots that did not arrive in time: those of `observe` calls, and the
    one taken after an input (lost with a connection, or with a Gateway that stopped
    between the input and the screenshot). The agent then looks again itharness."""
    for call in await runs.calls_for_run(conn, run.id):
        if call.output is None and call.action_id is not None:
            await _settle_follow_up(harness, conn, call.action_id, now)
            continue
        if call.output is not None or call.observation_id is None:
            continue
        obs = await execution.get_observation(conn, call.observation_id)
        if obs is None or obs.status in (ObservationStatus.COMPLETE, ObservationStatus.FAILED):
            continue
        if now - obs.requested_at > harness.limits.observation_timeout:
            await execution.fail_observation(conn, obs.id, "timeout")
            await runs.resolve_call(
                conn,
                call.id,
                {"status": "failed", "observation_id": str(obs.id), "error": "timeout"},
            )


async def _settle_follow_up(
    harness: "Harness", conn: AsyncConnection, action_id: UUID, now: datetime
) -> None:
    action = await execution.get_action(conn, action_id)
    if action is None or action.status != ActionStatus.DONE or action.finished_at is None:
        return
    post = await execution.get_observation(conn, action.post_observation_id)
    if post is None or post.status in (ObservationStatus.COMPLETE, ObservationStatus.FAILED):
        return
    if now - action.finished_at > harness.limits.observation_timeout:
        await execution.fail_observation(conn, post.id, "timeout")
        await exec_service.resolve_action_call(conn, action.id)


async def wait_reason(conn: AsyncConnection, open_calls: list[ToolCall]) -> WaitReason:
    reasons = set()
    for call in open_calls:
        if call.action_id is not None:
            action = await execution.get_action(conn, call.action_id)
            awaiting = action is not None and action.status == ActionStatus.AWAITING_APPROVAL
            reasons.add(WaitReason.APPROVAL if awaiting else WaitReason.ACTION)
        elif call.child_run_id is not None:
            reasons.add(WaitReason.CHILDREN)
        else:
            reasons.add(WaitReason.OBSERVATION)
    for reason in (
        WaitReason.APPROVAL,
        WaitReason.ACTION,
        WaitReason.OBSERVATION,
        WaitReason.CHILDREN,
    ):
        if reason in reasons:
            return reason
    return WaitReason.OBSERVATION


async def _done(
    conn: AsyncConnection, task: Task, run: Run, inputs: list[dict[str, Any]]
) -> list[tuple[Action, str | None]]:
    """For a main agent asked for a skill: the task's inputs so far, with the notes
    written with each."""
    asked = [task.request] + [str(i["text"]) for i in inputs if i["kind"] == "note"]
    if run.parent_run_id is not None or all(rules.skill_command(t) is None for t in asked):
        return []
    notes = {
        c.id: c.arguments.get("notes")
        for r in await runs.for_task(conn, task.id)
        for c in await runs.calls_for_run(conn, r.id)
    }
    return [
        (a, str(notes[a.tool_call_id]) if notes.get(a.tool_call_id) else None)
        for a in await execution.task_actions(conn, task.id)
    ]
