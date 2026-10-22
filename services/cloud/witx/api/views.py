"""Maps stored records to the API's response models."""

from collections.abc import Collection, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from witx_contracts import api

from witx.domain import rules
from witx.domain.model import (
    TERMINAL_TASK,
    Action,
    Approval,
    Device,
    Grant,
    Memory,
    Observation,
    Policy,
    PurchaseIntent,
    Run,
    Schedule,
    SideMessage,
    Task,
    TaskGroup,
)
from witx.store import devices, execution, purchases, runs, side_turns, tasks
from witx.store import memories as memory_store


def device_view(
    device: Device,
    task_id: UUID | None,
    pool: str,
    task_goal: str | None = None,
    *,
    taken_over: bool = False,
) -> api.DeviceView:
    return api.DeviceView(
        id=device.id,
        name=device.name,
        platform=device.platform,
        online=device.online,
        capabilities=device.capabilities,
        task_id=task_id,
        task_goal=task_goal if task_id else None,
        pool=pool,
        problems=device.problems,
        enabled=device.enabled,
        taken_over=taken_over,
    )


def group(group: TaskGroup) -> api.TaskGroupView:
    return api.TaskGroupView(id=group.id, name=group.name, created_at=group.created_at)


def summary(task: Task, needs_attention: bool, *, pinned: bool = False) -> api.TaskSummary:
    return api.TaskSummary(
        id=task.id,
        request=task.request or task.goal,
        goal=task.goal,
        title=task.title,
        status=task.status.value,
        permission_mode=task.permission_mode.value,
        needs_attention=needs_attention,
        group_id=task.group_id,
        pinned=pinned,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _result(data: dict[str, Any], fallback: datetime) -> api.TaskResult:
    """A finished round's outcome as the web shows it: the summary, the screenshots it
    cites and when it was done. A failed round gives its reason the same way, so the
    page can say why it failed."""
    return api.TaskResult(
        summary=data.get("summary", ""),
        evidence_observation_ids=data.get("evidence_observation_ids", []),
        completed_at=data.get("completed_at", fallback),
        failure=data.get("failure"),
        offer_skill=data.get("offer_skill", False),
    )


# The ends of a round that have a result to show: done, or failed with its reason.
_ENDED = frozenset({"completed", "failed"})


def _run(run: Run) -> api.RunView:
    done = run.status.value in _ENDED and isinstance((run.result or {}).get("summary"), str)
    return api.RunView(
        id=run.id,
        parent_run_id=run.parent_run_id,
        goal=run.goal,
        status=run.status.value,
        wait_reason=run.wait_reason.value if run.wait_reason else None,
        wait_detail=run.wait_detail,
        device_ids=run.device_ids,
        created_at=run.created_at,
        name=run.name,
        result=_result(run.result or {}, run.created_at) if done else None,
        saying=run.saying,
        phase=run.phase,
    )


def _approval(approval: Approval | None, now: datetime) -> api.ApprovalView | None:
    if approval is None:
        return None
    return api.ApprovalView(
        status=rules.approval_status(approval, now),
        expires_at=approval.expires_at,
        decided_at=approval.decided_at,
        grant_id=approval.grant_id,
    )


def grant_view(grant: Grant, task: Task, now: datetime) -> api.GrantView:
    status = rules.grant_status(grant, control_revision=task.control_revision, now=now)
    return api.GrantView(
        id=grant.id,
        created_from=grant.created_from,
        device_id=grant.device_id,
        app=grant.app,
        effects=grant.effects,
        terms=grant.terms,
        status="ended" if status == "active" and task.status in TERMINAL_TASK else status,
        uses=grant.uses,
        max_uses=grant.max_uses,
        expires_at=grant.expires_at,
        created_at=grant.created_at,
    )


def purchase_view(intent: PurchaseIntent, newest: bool) -> api.PurchaseView:
    return api.PurchaseView(
        id=intent.id,
        goal=intent.goal,
        options=[
            api.PurchaseOptionView(
                device_id=o.device_id,
                payee=o.payee,
                amount=o.amount,
                currency=o.currency,
                summary=o.summary,
                observation_id=o.observation_id,
            )
            for o in intent.options
        ],
        status="superseded" if intent.status == "proposed" and not newest else intent.status,
        chosen=intent.chosen,
        decided_at=intent.decided_at,
        created_at=intent.created_at,
    )


def _policy(policy: Policy | None) -> api.PolicyView | None:
    if policy is None:
        return None
    risk = policy.risk
    return api.PolicyView(
        mode=policy.mode.value,
        decision="ask" if policy.decision == "ask" else "allow",
        reasons=policy.reasons,
        risk=None
        if risk is None
        else api.RiskView(
            sensitive=risk.sensitive,
            effects=risk.effects,
            reason=risk.reason,
            failed=risk.failed,
            seen=risk.seen,
        ),
    )


def action_view(action: Action, approval: Approval | None, now: datetime) -> api.ActionView:
    return api.ActionView(
        id=action.id,
        run_id=action.run_id,
        device_id=action.device_id,
        kind=action.kind,
        input=action.input,
        target=action.target,
        expected_effect=action.expected_effect,
        effects=action.effects,
        status=action.status.value,
        effect=action.effect.value,
        reason=action.reason,
        observation_id=action.observation_id,
        post_observation_id=action.post_observation_id,
        approval=_approval(approval, now),
        policy=_policy(action.policy),
        intent=action.intent,
        retry_of=action.retry_of,
        created_at=action.created_at,
        finished_at=action.finished_at,
        changes=action.changes,
    )


def _observation(obs: Observation) -> api.ObservationView:
    return api.ObservationView(
        id=obs.id,
        device_id=obs.device_id,
        status=obs.status.value,
        captured_at=obs.captured_at,
        width=obs.screen.width if obs.screen else None,
        height=obs.screen.height if obs.screen else None,
        foreground_app=obs.screen.foreground_app if obs.screen else None,
    )


def memory_view(memory: Memory) -> api.MemoryView:
    return api.MemoryView.model_validate(
        {
            k: getattr(memory, k)
            for k in (
                "id",
                "text",
                "kind",
                "app",
                "by_agent",
                "source_task_id",
                "created_at",
                "decided_at",
            )
        }
    )


async def _plan(conn: AsyncConnection, task: Task) -> list[api.PlanStepView]:
    """The newest plan of the task's newest root run."""
    roots = [r for r in await runs.for_task(conn, task.id) if r.parent_run_id is None]
    if not roots:
        return []
    args = await runs.latest_plan_call(conn, max(roots, key=lambda r: r.created_at).id)
    try:
        plan = rules.check_plan((args or {}).get("steps") or [])
    except rules.Rejected:
        return []
    return [api.PlanStepView.model_validate(p) for p in plan]


# Records changed after an event, by kind: "actions", "observations", "runs".
Changed = dict[str, set[UUID]]

# The keys of an event's data that name a record a task's page shows, and its kind.
_NAMES = {
    "action_id": "actions",
    "observation_id": "observations",
    "run_id": "runs",
    "to_run_id": "runs",
    "parent_run_id": "runs",
}


async def _named(conn: AsyncConnection, task_id: UUID, since: int, through: int) -> Changed:
    """The actions, screenshots and runs that the task's events after ``since`` (up to
    ``through``) name: every write to one of them emits an event that names it."""
    named: Changed = {"actions": set(), "observations": set(), "runs": set()}
    while since < through:
        rows = await tasks.events_after(conn, task_id, since)
        if not rows:
            break
        for row in rows:
            if row["seq"] > through:
                return named
            for key, kind in _NAMES.items():
                value = row["data"].get(key)
                if isinstance(value, str):
                    with suppress(ValueError):
                        named[kind].add(UUID(value))
        since = rows[-1]["seq"]
    return named


async def task_detail(
    conn: AsyncConnection,
    task: Task,
    start: datetime | Literal["latest"] | None = "latest",
    types: Collection[str] = (),
) -> api.TaskDetail:
    """What the task's page shows, from ``start`` on: by default the latest part
    (``rules.part_start``), or from the time the page asks for, or (None) all of it.
    ``types``: the events of these kinds in that part come with it."""
    return api.TaskDetail(**await _detail(conn, task, start=start, types=types))


async def task_earlier(
    conn: AsyncConnection, task: Task, before: datetime, types: Collection[str] = ()
) -> api.TaskEarlier:
    """The part of the task before ``before``, by the rule of the first part."""
    last_event_seq = await tasks.last_event_seq(conn, task.id)
    run_list = await runs.for_task(conn, task.id)
    start = rules.part_start(
        [r.created_at for r in run_list if r.parent_run_id is None],
        await execution.action_times(conn, task.id),
        before=before,
    )
    now = datetime.now(UTC)
    actions = await execution.task_actions(conn, task.id, start=start, end=before)
    approvals = await execution.approvals_for(conn, [a.id for a in actions])
    shown = _shots_of(actions, [r for r in run_list if _within(r.created_at, start, before)])
    return api.TaskEarlier(
        actions=[action_view(a, approvals.get(a.id), now) for a in actions],
        observations=[
            _observation(o)
            for o in await execution.task_observations(
                conn, task.id, start=start, end=before, also=shown
            )
        ],
        notes=[_note(n) for n in await runs.task_notes(conn, task.id, start=start, end=before)],
        events=[
            _event(e)
            for e in await tasks.events_between(conn, task.id, types, start, before, last_event_seq)
        ],
        earlier=api.EarlierView(before=start) if start else None,
    )


def _within(at: datetime, start: datetime | None, end: datetime | None) -> bool:
    return (start is None or at >= start) and (end is None or at < end)


def _shots_of(actions: Sequence[Action], run_list: Sequence[Run]) -> set[UUID]:
    """The screenshots these actions were based on and left, and those these runs'
    results show."""
    shots = {i for a in actions for i in (a.observation_id, a.post_observation_id) if i}
    for run in run_list:
        for i in (run.result or {}).get("evidence_observation_ids", []):
            with suppress(ValueError, TypeError):
                shots.add(UUID(str(i)))
    return shots


def _note(n: RowMapping) -> api.NoteView:
    return api.NoteView(
        text=str(n["text"])[:1000],
        run_id=n["run_id"],
        observation_id=n["observation_id"],
        seen_at=n["captured_at"],
    )


def _event(row: RowMapping) -> api.EventView:
    return api.EventView(
        seq=row["seq"], type=row["type"], data=row["data"], created_at=row["created_at"]
    )


async def task_changes(
    conn: AsyncConnection, task: Task, since: int, held_open: Collection[UUID] = ()
) -> api.TaskChanges:
    """What changed after the event ``since``: see ``api.TaskChanges``. ``held_open`` are
    the records the page holds as not finished: they come too, as some finish with no
    event that names them (a run or input cancelled as the task stops, a screenshot that
    timed out)."""
    return api.TaskChanges(
        **await _detail(conn, task, since=since, held_open=held_open), since=since
    )


async def _detail(
    conn: AsyncConnection,
    task: Task,
    *,
    since: int | None = None,
    held_open: Collection[UUID] = (),
    start: datetime | Literal["latest"] | None = None,
    types: Collection[str] = (),
) -> dict[str, Any]:
    # Read first: a change committed after this has a later event, so the next changes
    # from here include it, while a change seen below and named again is only sent twice.
    last_event_seq = await tasks.last_event_seq(conn, task.id)
    changed = None if since is None else await _named(conn, task.id, since, last_event_seq)
    if changed is not None:
        for ids in changed.values():
            ids |= set(held_open)

    def only(kind: str) -> set[UUID] | None:
        return None if changed is None else changed[kind]

    run_list = await runs.for_task(conn, task.id, changed=only("runs"))
    if start == "latest":
        start = rules.part_start(
            [r.created_at for r in run_list if r.parent_run_id is None],
            await execution.action_times(conn, task.id),
        )
    if changed is not None or (start is not None and start <= task.created_at):
        start = None  # changes are not in parts; a part from the start is all of it
    now = datetime.now(UTC)
    actions = await execution.task_actions(conn, task.id, changed=only("actions"), start=start)
    approvals = await execution.approvals_for(conn, [a.id for a in actions])
    shown: set[UUID] = set()
    if start is not None:
        shown = _shots_of(actions, [r for r in run_list if r.created_at >= start])
        shown |= set(await execution.latest_shots(conn, task.id))
    intents = await purchases.for_task(conn, task.id)
    # Which of the person's messages the main agent has not read yet.
    read_through = await runs.read_through(conn, task.id)
    seen = await side_turns.seen_inputs(conn, task.id)
    result = None
    if task.result and task.status.value in _ENDED:
        result = _result(task.result, task.updated_at)
    # Time counts only while the agent works (not while it waits for you), and for the
    # goal only while it is set and not paused.
    spans, working = await tasks.work_spans(conn, task.id, now)
    changes = await tasks.goal_changes(conn, task.id)
    first = changes[0] if changes else None
    from_start = (first["kind"] != "set" or bool(first["previous"])) if first else bool(task.goal)
    windows = rules.goal_windows(
        task.created_at, from_start, [(c["at"], c["kind"]) for c in changes], now
    )
    return dict(
        id=task.id,
        created_by=await tasks.creator(conn, task.id),
        goal_paused=task.goal_paused,
        plan_mode=task.plan_mode,
        agent_ms=rules.active_ms(spans, [(task.created_at, now)]),
        goal_ms=rules.active_ms(spans, windows) if task.goal else 0,
        working=working,
        request=task.request or task.goal,
        goal=task.goal,
        title=task.title,
        constraints=task.constraints,
        device_ids=task.device_ids,
        status=task.status.value,
        revision=task.revision,
        model=task.model,
        reasoning_effort=task.reasoning_effort,
        permission_mode=task.permission_mode.value,
        usage=api.UsageView(**dict(await runs.task_usage(conn, task.id))),
        result=result,
        inputs=[
            api.TaskInputView(
                seq=i["seq"],
                kind=i["kind"],
                text=i["text"],
                created_at=i["created_at"],
                run_id=i["run_id"],
                created_by=i["created_by"],
                queued=rules.queued_input(
                    kind=i["kind"],
                    run_id=i["run_id"],
                    seq=i["seq"],
                    read_through=read_through,
                    seen=seen,
                    live=task.status.value in ("active", "pausing", "paused"),
                ),
            )
            for i in await tasks.inputs(conn, task.id)
        ],
        runs=[_run(r) for r in run_list],
        actions=[action_view(a, approvals.get(a.id), now) for a in actions],
        grants=[grant_view(g, task, now) for g in await execution.task_grants(conn, task.id)],
        purchases=[purchase_view(p, i == len(intents) - 1) for i, p in enumerate(intents)],
        plan=await _plan(conn, task),
        memories=[memory_view(m) for m in await memory_store.from_task(conn, task.id)],
        notes=[
            _note(n)
            for n in await runs.task_notes(
                conn,
                task.id,
                runs=None if changed is None else [r.id for r in run_list],
                start=start,
            )
        ],
        observations=[
            _observation(o)
            for o in await execution.task_observations(
                conn, task.id, changed=only("observations"), start=start, also=shown
            )
        ],
        events=[
            _event(e)
            for e in await tasks.events_between(conn, task.id, types, start, None, last_event_seq)
        ]
        if types and changed is None
        else [],
        earlier=api.EarlierView(before=start) if start else None,
        last_event_seq=last_event_seq,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def device_views(conn: AsyncConnection) -> list[api.DeviceView]:
    all_devices = await devices.list_all(conn)
    now = datetime.now(UTC)
    ids = [d.id for d in all_devices]
    held = {lease.device_id: lease for lease in await devices.leases(conn, ids)}
    leases = {d: lease.task_id for d, lease in held.items() if lease.task_id is not None}
    pools = await devices.pool_names(conn)
    goals = await tasks.goals(conn, list(leases.values()))
    return [
        device_view(
            d,
            leases.get(d.id),
            pools.get(d.gateway_id, ""),
            goals.get(leases[d.id]) if d.id in leases else None,
            taken_over=d.id in held and rules.takeover_active(held[d.id], now),
        )
        for d in all_devices
    ]


def gateway_state(row: dict[str, object]) -> api.GatewayState:
    if row["revoked_at"]:
        return "revoked"
    if row["disabled_at"]:
        return "disabled"
    return "enrolled" if row["enrolled_at"] else "waiting"


async def gateway_views(conn: AsyncConnection, connected: set[UUID]) -> list[api.GatewayView]:
    """The tenant's device pools: live ones first, revoked ones last."""
    counts = await devices.device_counts(conn)
    views = [
        api.GatewayView(
            id=row["id"],
            name=str(row["name"]),
            host=row["host"],
            state=gateway_state(row),
            connected=row["id"] in connected,
            devices=counts.get(row["id"], 0),  # type: ignore[call-overload]
            last_seen_at=row["last_seen_at"],
            enroll_expires_at=row["enroll_expires_at"] if not row["enrolled_at"] else None,
        )
        for row in await devices.list_gateways(conn)
    ]
    order = {"enrolled": 0, "waiting": 1, "disabled": 2, "revoked": 3}
    return sorted(views, key=lambda g: (order[g.state], not g.connected, g.name))


def schedule(s: Schedule) -> api.ScheduleView:
    return api.ScheduleView(
        id=s.id,
        goal=s.goal,
        constraints=s.constraints,
        device_ids=s.device_ids,
        permission_mode=s.permission_mode.value,
        model=s.model,
        reasoning_effort=s.reasoning_effort,
        repeat=s.repeat.value,
        time_of_day=s.time_of_day,
        timezone=s.timezone,
        weekdays=s.weekdays,
        month_day=s.month_day,
        run_on=s.run_on,
        enabled=s.enabled,
        next_run_at=s.next_run_at,
        last_run_at=s.last_run_at,
        last_task_id=s.last_task_id,
        last_outcome=s.last_outcome,
    )


def side_message(m: SideMessage) -> api.SideMessageView:
    return api.SideMessageView(
        id=m.id,
        thread_id=m.thread_id,
        question=m.question,
        answer=m.answer,
        error=m.error,
        created_at=m.created_at,
        answered_at=m.answered_at,
    )
