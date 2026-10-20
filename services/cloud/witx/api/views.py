"""Maps stored records to the API's response models."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

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


async def task_detail(conn: AsyncConnection, task: Task) -> api.TaskDetail:
    now = datetime.now(UTC)
    actions = await execution.task_actions(conn, task.id)
    approvals = await execution.approvals_for(conn, [a.id for a in actions])
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
    return api.TaskDetail(
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
        runs=[_run(r) for r in await runs.for_task(conn, task.id)],
        actions=[action_view(a, approvals.get(a.id), now) for a in actions],
        grants=[grant_view(g, task, now) for g in await execution.task_grants(conn, task.id)],
        purchases=[purchase_view(p, i == len(intents) - 1) for i, p in enumerate(intents)],
        plan=await _plan(conn, task),
        memories=[memory_view(m) for m in await memory_store.from_task(conn, task.id)],
        notes=[
            api.NoteView(
                text=str(n["text"])[:1000],
                observation_id=n["observation_id"],
                seen_at=n["captured_at"],
            )
            for n in await runs.task_notes(conn, task.id)
        ],
        observations=[_observation(o) for o in await execution.task_observations(conn, task.id)],
        last_event_seq=await tasks.last_event_seq(conn, task.id),
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
