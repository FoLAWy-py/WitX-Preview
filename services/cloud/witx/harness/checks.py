"""The independent checks of a turn, outside any transaction: the risk of its input and, under a
goal, whether the goal holds at its end."""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from witx.domain import rules
from witx.domain.model import Action, Observation, ObservationStatus, RiskVerdict
from witx.domain.rules import Rejected
from witx.harness import tools
from witx.harness.risk import RiskCheck
from witx.harness.turn import Prepared, calls_of, reply_text, utcnow
from witx.harness.verify import GoalCheck, Judgement, Shot
from witx.models import ModelResponse
from witx.store import execution, identity, tasks

if TYPE_CHECKING:
    from witx.harness.loop import Harness


async def judge_goal(
    harness: "Harness", prepared: Prepared, response: ModelResponse
) -> Judgement | None:
    """Whether the task's goal holds at this turn's end, outside any transaction:
    judged when the agent finishes, and when its turn ends in words without a tool call
    (it has stopped; is it done?). None without a goal, or a judge, or anything to
    judge. The agent's answer itself is not checked: what the model says is final."""
    if harness.judge is None:
        return None
    goal = await _goal_check(harness, prepared)
    if goal is None:
        return None
    item = next(
        (
            i
            for i in response.output
            if i.get("type") == "function_call" and i.get("name") == "complete_task"
        ),
        None,
    )
    task, run = prepared.task, prepared.run
    if item is not None:
        try:
            args = json.loads(item.get("arguments") or "{}")
            ids = [UUID(str(i)) for i in args.get("evidence_observation_ids") or []]
        except (ValueError, TypeError, AttributeError):
            return None  # the completion is refused for its arguments anyway
        words = str(args.get("summary", ""))[:4000]
    else:
        replied = rules.shown_text(reply_text(response))
        if not replied or calls_of(response):
            return None
        ids, words = [], replied[:4000]  # the reply says where the agent stands
    async with harness.db.tenant(task.tenant_id) as conn:
        if item is None:  # the newest screenshot of each phone
            latest = [await execution.latest_complete(conn, d) for d in run.device_ids]
            ids = [o.id for o in latest if o is not None and o.task_id == task.id]
        shots = [
            o
            for o in await execution.observations(conn, ids)
            if o.task_id == task.id
            and o.status == ObservationStatus.COMPLETE
            and o.object_key is not None
        ]
        inputs = await tasks.inputs(conn, task.id)
        actions = await execution.task_actions(conn, task.id)
        last_input = rules.last_inputs(actions)
    shots.sort(key=lambda o: o.captured_at or datetime.min.replace(tzinfo=UTC))
    request = task.request or task.goal
    # What the user said since: the user's own words only, not the main agent's to a
    # sub-agent, and only what this step read.
    later = [
        f"- {i['text']}"
        for i in inputs
        if 1 < int(i["seq"]) <= prepared.input_seq and i["kind"] != "agent" and i["run_id"] is None
    ]
    if later:
        request += "\nThe user's later messages and constraints, in order:\n"
        request += "\n".join(later)
    return await harness.judge.judge(
        model=task.model,
        request=request,
        words=words,
        goal=goal,
        screenshots=[
            Shot(
                o.id,
                await harness.objects.get(str(o.object_key)),
                o.device_id not in last_input
                or (o.captured_at is not None and o.captured_at > last_input[o.device_id]),
                readings(o, actions),
            )
            for o in shots
        ],
        allowed_providers=prepared.allowed_providers,
        reasoning_effort=task.reasoning_effort,
    )


def readings(observation: Observation, actions: list[Action]) -> list[str]:
    """What the phone reported with a screenshot, as the agent was told it: the sliders in
    view with the position each reports, and the text the field held after the
    input this screenshot followed. The judge sees the same."""
    found = []
    for slider in observation.screen.sliders if observation.screen else []:
        name = str(slider.get("name") or "").strip()
        found.append(
            f"slider {name!r}: {slider.get('value')}"
            if name
            else f"a slider: {slider.get('value')}"
        )
    for action in actions:
        if action.post_observation_id == observation.id and action.field_text:
            name = str((action.field_info or {}).get("name") or "").strip()
            field = f"field {name!r}" if name else "the field typed into"
            found.append(f"{field} held: {action.field_text!r}")
    return found


async def _goal_check(harness: "Harness", prepared: Prepared) -> GoalCheck | None:
    """The goal to judge at this turn's end: the root run's, while it is set and the
    user has not paused it."""
    task = prepared.task
    if prepared.run.parent_run_id is not None or not task.goal or task.goal_paused:
        return None
    async with harness.db.tenant(task.tenant_id) as conn:
        evaluations, since = await tasks.goal_progress(conn, task.id)
    return GoalCheck(task.goal, evaluations, int((utcnow() - since).total_seconds()))


async def assess(
    harness: "Harness", prepared: Prepared, response: ModelResponse
) -> tuple[str, RiskCheck] | None:
    """Runs the independent risk check for this turn's input, outside any transaction.

    Only the first input call can be admitted per turn, so only it is assessed.
    """
    item = next(
        (
            i
            for i in response.output
            if i.get("type") == "function_call" and i.get("name") in tools.INPUT_TOOLS
        ),
        None,
    )
    if item is None:
        return None
    try:
        args = json.loads(item.get("arguments") or "{}")
        parsed = tools.parse_input(str(item["name"]), args if isinstance(args, dict) else {})
    except (ValueError, Rejected):
        return None  # admission rejects it anyway
    if parsed.payload["kind"] not in rules.ASSESSED_KINDS:
        return None
    call_id = str(item.get("call_id"))
    async with harness.db.tenant(prepared.task.tenant_id) as conn:
        obs = await execution.get_observation(conn, parsed.observation_id)
        fast = await identity.fast_risk_check(conn, prepared.task.tenant_id)
    if obs is None or obs.object_key is None or obs.screen is None:
        return None  # admission rejects inputs without a complete screenshot
    if harness.risk is None:
        verdict = RiskVerdict(
            sensitive=True, effects=[], reason="no risk assessor configured", failed=True
        )
        return call_id, RiskCheck(verdict, None)
    check = await harness.risk.assess(
        model=prepared.task.model,  # the task's model, switched together with it
        observation_id=obs.id,
        screenshot=await harness.objects.get(obs.object_key),
        payload=parsed.payload,
        foreground_app=obs.screen.foreground_app,
        target=parsed.target,
        expected_effect=parsed.expected_effect,
        fast=fast,  # an owner's choice
        allowed_providers=prepared.allowed_providers,
    )
    return call_id, check
