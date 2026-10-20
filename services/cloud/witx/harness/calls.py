"""The agent's tool calls of a turn: each tool's handler, inside the turn's transaction.

A handler resolves its call, links it to what it waits on (an action, a screenshot,
sub-agents), or raises Rejected; what the turn's end depends on goes into the Outcome.
"""

from collections.abc import Awaitable, Callable, Collection
from datetime import date
from datetime import time as clock_time
from typing import Any
from uuid import UUID, uuid4

from witx.domain import rules
from witx.domain.model import TERMINAL_RUN, ObservationStatus, Repeat, ToolCall
from witx.domain.rules import Rejected
from witx.harness import plugins, tools
from witx.harness.turn import Turn, utcnow
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.services import memories as memory_service
from witx.services import purchases as purchase_service
from witx.services import schedules as schedule_service
from witx.store import devices, execution, runs, tasks

Handler = Callable[[Turn, ToolCall], Awaitable[None]]


async def handle(turn: Turn, call: ToolCall) -> None:
    """Handles one tool call; a refusal resolves it with the rule's code."""
    handler = HANDLERS.get(call.name)
    try:
        if handler is None:
            raise Rejected("unknown_tool", call.name)
        await handler(turn, call)
    except Rejected as error:
        await runs.resolve_call(
            turn.conn,
            call.id,
            {"status": "rejected", "code": error.code, "detail": error.detail},
        )
        turn.outcome.repeating = turn.outcome.repeating or error.code == "repeating"


async def _input(turn: Turn, call: ToolCall) -> None:
    outcome = turn.outcome
    if outcome.input_seen:
        raise Rejected("one_input_per_turn", "wait for the previous input's result")
    outcome.input_seen = True
    parsed = tools.parse_input(call.name, call.arguments)
    verdict = turn.verdict
    await exec_service.admit(
        turn.conn,
        task=turn.task,
        run=turn.run,
        call=call,
        device_id=parsed.device_id,
        payload=parsed.payload,
        observation_id=parsed.observation_id,
        target=parsed.target,
        expected_effect=parsed.expected_effect,
        effects=parsed.effects,
        intent=parsed.intent,
        changes=parsed.changes,
        risk=verdict[1].verdict if verdict and verdict[0] == call.call_id else None,
        limits=turn.harness.limits,
        now=utcnow(),
    )


async def _load_skill(turn: Turn, call: ToolCall) -> None:
    found = turn.harness.skills()
    full = rules.named_skill(str(call.arguments.get("name", "")).removeprefix("$"), found)
    if full is None:
        raise Rejected("unknown_skill", "load one of the skills listed, by its full name")
    skill = found[full]
    if skill.invoke == "user" and full not in await _named_by_user(turn, found):
        raise Rejected("unknown_skill", "load one of the skills listed, by its full name")
    await runs.resolve_call(
        turn.conn,
        call.id,
        {
            "status": "loaded",
            "name": skill.name,
            "version": skill.version,
            "sha256": skill.digest,
            "content": skill.body,
        },
    )
    await tasks.emit(
        turn.conn,
        turn.task,
        "skill.loaded",
        run_id=turn.run.id,
        name=skill.name,
        version=skill.version,
    )


async def _named_by_user(turn: Turn, found: Collection[str]) -> set[str]:
    """The skills the user named in this task: in the request or a message."""
    said = [turn.task.request] + [
        str(i["text"]) for i in await tasks.inputs(turn.conn, turn.task.id) if i["kind"] == "note"
    ]
    return {
        full
        for text in said
        for name in rules.named_skills(text)
        if (full := rules.named_skill(name, found)) is not None
    }


async def _draft_skill(turn: Turn, call: ToolCall) -> None:
    """A skill for the person to save: shown in the conversation, never saved or
    loaded by the agent."""
    if turn.run.parent_run_id is not None:
        raise Rejected("not_allowed", "only the main agent drafts skills")
    args = call.arguments
    apps = args.get("apps")
    try:
        _, skill = plugins.draft(
            str(args.get("name", "")).strip(),
            rules.shown_text(str(args.get("description", ""))),
            [str(a) for a in apps] if isinstance(apps, list) else [],
            rules.shown_text(str(args.get("body", ""))),
        )
    except ValueError as error:
        raise Rejected("invalid_skill", str(error)) from None
    saved = turn.harness.skills().get(skill.name)
    await tasks.emit(
        turn.conn,
        turn.task,
        "skill.drafted",
        run_id=turn.run.id,
        name=skill.name,
        description=skill.description,
        apps=list(skill.apps),
        body=skill.body,
    )
    await runs.resolve_call(
        turn.conn,
        call.id,
        {
            "status": "drafted",
            "skill": skill.name,
            "note": "The user reads it and saves it themselves, or tells you what to change; "
            + ("saved, it replaces their skill of this name." if saved else "you cannot save it."),
        },
    )


async def _set_goal(turn: Turn, call: ToolCall) -> None:
    goal = str(call.arguments.get("goal", "")).strip()[:4000]
    if not goal:
        raise Rejected("invalid_arguments", "state the goal")
    await lifecycle.agent_set_goal(turn.conn, turn.task, goal)
    await runs.resolve_call(turn.conn, call.id, {"status": "set"})


async def _remember(turn: Turn, call: ToolCall) -> None:
    replaces = call.arguments.get("replaces")
    saved = await memory_service.save(
        turn.conn,
        turn.task,
        turn.run,
        kind=str(call.arguments.get("kind", "")),
        app=call.arguments.get("app") or None,
        text=str(call.arguments.get("text", "")),
        replaces=tools.uuid_arg(call.arguments, "replaces") if replaces else None,
    )
    await runs.resolve_call(
        turn.conn,
        call.id,
        {
            "status": "saved",
            "memory_id": str(saved.id),
            "note": "Kept for this user's future tasks; they can undo it.",
        },
    )


async def _update_plan(turn: Turn, call: ToolCall) -> None:
    raw = call.arguments.get("steps")
    plan = rules.check_plan(raw if isinstance(raw, list) else [])
    await runs.resolve_call(turn.conn, call.id, {"status": "ok"})
    await tasks.emit(turn.conn, turn.task, "run.plan", run_id=turn.run.id, steps=plan)


async def _propose_purchase(turn: Turn, call: ToolCall) -> None:
    intent = await purchase_service.propose(
        turn.conn,
        turn.task,
        turn.run,
        goal=str(call.arguments.get("goal", "")),
        options=tools.parse_purchase_options(call.arguments),
        now=utcnow(),
    )
    await runs.resolve_call(
        turn.conn,
        call.id,
        {
            "status": "proposed",
            "purchase_id": str(intent.id),
            "note": "Wait for the user's choice; it comes as their next message.",
        },
    )
    question = rules.shown_text(str(call.arguments.get("question", "")))
    turn.outcome.question = question[:2000] or intent.goal


async def _use_web_search(turn: Turn, call: ToolCall) -> None:
    reason = str(call.arguments.get("reason", ""))[:300]
    await runs.resolve_call(turn.conn, call.id, {"status": "enabled"})
    await tasks.emit(turn.conn, turn.task, "agent.web_search_on", run_id=turn.run.id, reason=reason)


async def _present_plan(turn: Turn, call: ToolCall) -> None:
    if not turn.task.plan_mode:
        raise Rejected("not_planning", "plan mode is off: do the work")
    if turn.run.parent_run_id is not None:
        raise Rejected("not_planning", "only the main agent presents the plan")
    if turn.outcome.calls > 1:
        raise Rejected("invalid_arguments", "call present_plan alone")
    plan_text = rules.shown_text(str(call.arguments.get("plan", "")))[:8000]
    if not plan_text.strip():
        raise Rejected("invalid_arguments", "write the plan")
    await runs.resolve_call(
        turn.conn,
        call.id,
        {
            "status": "presented",
            "note": "The user decides; their answer comes as a message. Do "
            "not act until they approve.",
        },
    )
    await tasks.emit(turn.conn, turn.task, "plan.proposed", run_id=turn.run.id, text=plan_text)
    turn.outcome.plan = plan_text


async def _ask_user(turn: Turn, call: ToolCall) -> None:
    outcome = turn.outcome
    outcome.question = rules.shown_text(str(call.arguments.get("question", "")))[:2000]
    outcome.options, outcome.recommended = rules.ask_options(
        call.arguments.get("options"), call.arguments.get("recommended")
    )
    # A sub-agent may ask the main agent instead of the user.
    outcome.ask_main = turn.run.parent_run_id is not None and call.arguments.get("to") == "main"
    await runs.resolve_call(
        turn.conn,
        call.id,
        {
            "status": "asked",
            "to": "main agent",
            "note": "Its answer comes as your next message; it is not the "
            "user's and grants no permission.",
        }
        if outcome.ask_main
        else {"status": "asked"},
    )


async def _side_turn_only(turn: Turn, call: ToolCall) -> None:
    raise Rejected(
        "not_in_side_turn",
        "only in a side turn while your sub-agents work; none is working now",
    )


async def _complete_task(turn: Turn, call: ToolCall) -> None:
    turn.outcome.completed = await _complete(turn, call)


async def _observe(turn: Turn, call: ToolCall) -> None:
    conn, task, run = turn.conn, turn.task, turn.run
    device_id = tools.uuid_arg(call.arguments, "device_id")
    lease = await devices.lease(conn, device_id)
    if device_id not in run.device_ids or lease is None or lease.run_id != run.id:
        raise Rejected("no_device_lease", "this run does not control the device")
    device = await devices.get(conn, device_id)
    if device is None:
        raise Rejected("device_offline")
    rules.require_usable(device)  # e.g. not responding: the screenshot would never come
    obs = await exec_service.request_observation(
        conn, task=task, device_id=device_id, epoch=lease.epoch
    )
    await runs.link_call(conn, call.id, observation_id=obs.id)


async def _propose_schedule(turn: Turn, call: ToolCall) -> None:
    """A schedule the user asked for, off until they turn it on."""
    conn, task = turn.conn, turn.task
    args = call.arguments
    try:
        hours, minutes = str(args.get("time_of_day", "")).split(":")
        at = clock_time(int(hours), int(minutes))
        run_on = date.fromisoformat(str(args["run_on"])) if args.get("run_on") else None
        repeat = Repeat(str(args.get("repeat")))
        weekdays = [int(d) for d in args.get("weekdays") or []]
        month_day = int(args["month_day"]) if args.get("month_day") is not None else None
    except (ValueError, TypeError, KeyError):
        raise Rejected("invalid_arguments", "time_of_day is HH:MM, run_on YYYY-MM-DD") from None
    schedule = await schedule_service.propose(
        conn,
        task,
        goal=str(args.get("goal", "")),
        repeat=repeat,
        time_of_day=at,
        weekdays=weekdays,
        month_day=month_day,
        run_on=run_on,
        now=utcnow(),
    )
    await tasks.emit(
        conn,
        task,
        "schedule.proposed",
        schedule_id=str(schedule.id),
        goal=schedule.goal,
        repeat=schedule.repeat.value,
        time_of_day=schedule.time_of_day.strftime("%H:%M"),
        weekdays=schedule.weekdays,
        month_day=schedule.month_day,
        run_on=schedule.run_on.isoformat() if schedule.run_on else None,
    )
    await runs.resolve_call(
        conn,
        call.id,
        {
            "status": "proposed",
            "schedule_id": str(schedule.id),
            "note": "off until the user turns it on",
        },
    )


async def _view(turn: Turn, call: ToolCall) -> None:
    """An earlier screenshot of the task, attached to the next request only."""
    conn, task = turn.conn, turn.task
    obs_id = tools.uuid_arg(call.arguments, "observation_id")
    obs = await execution.get_observation(conn, obs_id)
    if (
        obs is None
        or obs.task_id != task.id
        or obs.status != ObservationStatus.COMPLETE
        or obs.object_key is None
        or obs.captured_at is None
    ):
        raise Rejected("unknown_screenshot", "name a screenshot of this task from the context")
    await runs.resolve_call(
        conn,
        call.id,
        {
            "status": "ok",
            "viewed_observation_id": str(obs.id),
            "device_id": str(obs.device_id),
            "captured_at": obs.captured_at.isoformat(),
        },
    )


async def _delegate(turn: Turn, call: ToolCall) -> None:
    """Starts one child run per subtask, all at once; the call resolves when every one
    has finished (see lifecycle.finish_run)."""
    harness, conn, task, run = turn.harness, turn.conn, turn.task, turn.run
    if run.depth + 1 > harness.limits.max_child_depth:
        raise Rejected("too_deep", "sub-agents cannot delegate further")
    raw = call.arguments.get("subtasks")
    if not isinstance(raw, list) or not raw:
        raise Rejected("invalid_arguments", "subtasks must list at least one sub-goal")
    existing = await runs.children(conn, run.id)
    if len(existing) + len(raw) > harness.limits.max_children_per_run:
        raise Rejected("too_many_children")
    subtasks: list[tuple[str, list[UUID], str | None]] = []
    handed: set[UUID] = set()
    for entry in raw:
        goal = str(entry.get("goal", "")).strip() if isinstance(entry, dict) else ""
        if not goal:
            raise Rejected("invalid_arguments", "every subtask needs a goal")
        device_ids: list[UUID] = []
        if entry.get("device_id"):
            device_id = tools.uuid_arg(entry, "device_id")
            lease = await devices.lease(conn, device_id)
            if lease is None or lease.run_id != run.id:
                raise Rejected("no_device_lease", "you can only hand over a device you control")
            if device_id in handed:
                raise Rejected("device_handed_twice", "give each device to one sub-agent")
            handed.add(device_id)
            device_ids = [device_id]
        subtasks.append((goal, device_ids, rules.agent_name(entry.get("name"))))
    children = []
    for index, (goal, device_ids, name) in enumerate(subtasks):
        child = await runs.insert(
            conn,
            id=uuid4(),
            tenant_id=task.tenant_id,
            task_id=task.id,
            goal=goal,
            device_ids=device_ids,
            parent_run_id=run.id,
            spawn_key=f"{call.call_id}:{index}",
            depth=run.depth + 1,
            name=name,
        )
        if child is None:
            raise Rejected("duplicate_delegate")
        children.append(child)
        for device_id in device_ids:
            await lifecycle.lend_device(conn, task=task, child=child, device_id=device_id)
        await tasks.emit(conn, task, "run.created", run_id=child.id, parent_run_id=run.id)
    # The call waits on its children (wait reason "children").
    await runs.link_call(conn, call.id, child_run_id=children[0].id)


async def _complete(turn: Turn, call: ToolCall) -> dict[str, Any] | None:
    conn, task, run, outcome = turn.conn, turn.task, turn.run, turn.outcome
    raw_ids = call.arguments.get("evidence_observation_ids") or []
    try:
        evidence_ids = [UUID(str(i)) for i in raw_ids]
    except ValueError:
        raise Rejected("invalid_arguments", "evidence_observation_ids must be IDs") from None
    evidence = [o for o in await execution.observations(conn, evidence_ids) if o.task_id == task.id]
    if len(evidence) != len(set(evidence_ids)):
        raise Rejected("invalid_arguments", "unknown observation ID")
    actions = [a for a in await execution.task_actions(conn, task.id) if a.run_id == run.id]
    gaps = rules.completion_gaps(actions=actions, evidence=evidence, device_ids=run.device_ids)
    if any(c.status not in TERMINAL_RUN for c in await runs.children(conn, run.id)):
        gaps.append("a sub-agent is still running")
    if gaps:
        await runs.resolve_call(conn, call.id, {"status": "rejected", "gaps": gaps})
        return None
    judged = outcome.goal
    if judged is not None and judged.verdict != "met":
        refused = sum(
            1
            for c in await runs.calls_for_run(conn, run.id)
            if c.name == "complete_task" and "goal" in (c.output or {})
        )
        await runs.resolve_call(
            conn,
            call.id,
            {
                "status": "rejected",
                "goal": {"verdict": judged.verdict, "reason": judged.reason},
                "note": "An independent check says the goal is not met yet. Keep "
                "working toward it; finish again when it holds. If you are stuck, "
                "`ask_user` with the options you see and the one you recommend.",
            },
        )
        if judged.verdict == "not_met" and refused >= rules.COMPLETION_REFUSALS:
            outcome.question = (
                f"The goal is still not met: {judged.reason} Tell me how to go on, or "
                "change the goal."
            )
        return None
    await runs.resolve_call(conn, call.id, {"status": "accepted"})
    return {
        "summary": rules.shown_text(str(call.arguments.get("summary", "")))[:4000],
        "evidence_observation_ids": [str(i) for i in evidence_ids],
        "completed_at": utcnow().isoformat(),
    }


HANDLERS: dict[str, Handler] = {
    "observe": _observe,
    "view_screenshot": _view,
    **{name: _input for name in tools.INPUT_TOOLS},
    "delegate": _delegate,
    "load_skill": _load_skill,
    "draft_skill": _draft_skill,
    "set_goal": _set_goal,
    "propose_schedule": _propose_schedule,
    "remember": _remember,
    "update_plan": _update_plan,
    "propose_purchase": _propose_purchase,
    "use_web_search": _use_web_search,
    "present_plan": _present_plan,
    "ask_user": _ask_user,
    "message_agent": _side_turn_only,  # see relay
    "stop_agent": _side_turn_only,
    "complete_task": _complete_task,
}
