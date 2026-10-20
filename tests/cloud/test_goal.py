"""A goal is a condition to be met: an independent judge decides whether it holds
when the agent finishes or ends its turn with a reply, and the work goes on until it holds,
can never hold, or the agent is getting nowhere. A task without a goal is not judged: what
the agent answers is final."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from witx.domain import rules
from witx.domain.model import GoalVerdict, RunStatus, TaskStatus, WaitReason
from witx.harness.verify import Judgement, parse_verdict
from witx.services import lifecycle
from witx.store import tasks
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision, say
from tests.cloud.test_flow import complete, last_screenshot

GOAL = "/goal The Wi-Fi settings page is open"


def judged(verdict: str, reason: str = "") -> GoalVerdict:
    return GoalVerdict(verdict, reason)  # type: ignore[arg-type]


@dataclass
class FakeJudge:
    """Answers each judgement with the next verdict (the last one repeats)."""

    answers: list[GoalVerdict | None]
    asked: list[dict[str, Any]] = field(default_factory=list)

    async def judge(self, **kw: Any) -> Judgement:
        self.asked.append(kw)
        answer = self.answers[min(len(self.asked) - 1, len(self.answers) - 1)]
        return Judgement(answer, {"model": kw["model"], "usage": {"input_tokens": 100}})


async def started(
    db: Database, admin_db: Database, tmp_path: Path, answers: list[GoalVerdict], script: list[Any]
) -> tuple[Rig, Any, FakeJudge]:
    tenant = await provision(admin_db, "acme", devices=1)
    judge = FakeJudge(list(answers))
    rig = Rig(db, tenant, tmp_path, judge=judge)
    await rig.connect([device_info(0)])
    rig.model.script = script
    task = await rig.create_task(GOAL)
    await rig.settle()
    return rig, task, judge


def reply(request: Any) -> list[dict[str, Any]]:
    return [say("I have opened the Wi-Fi settings.")]


async def test_a_completion_while_the_goal_is_not_met_goes_back_to_work(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, _ = await started(
        db,
        admin_db,
        tmp_path,
        [judged("not_met", "the Wi-Fi switch is still off"), judged("met")],
        [complete, complete],
    )
    first, second = [o for o in await rig.outputs(task.id) if o["name"] == "complete_task"]
    assert first["status"] == "rejected"
    assert first["goal"] == {"verdict": "not_met", "reason": "the Wi-Fi switch is still off"}
    assert second["status"] == "accepted"
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED


async def test_a_goal_that_is_met_is_over_and_the_task_has_none_any_more(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, _ = await started(db, admin_db, tmp_path, [judged("met", "it is open")], [complete])
    assert (await rig.task(task.id)).goal == ""
    events = await rig.event_types(task.id)
    assert events.count("goal.evaluated") == 1 and "goal.achieved" in events
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert (await tasks.goal_progress(conn, task.id))[0] == 0  # counted from when it was set


async def test_a_reply_without_a_tool_while_the_goal_is_not_met_sends_the_agent_back(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, _ = await started(
        db,
        admin_db,
        tmp_path,
        [judged("not_met", "the page is still the home screen"), judged("met")],
        [reply, complete],
    )
    second_turn = str(rig.model.requests[1].input)
    assert "the goal is not met yet: the page is still the home screen" in second_turn
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED


async def test_a_reply_without_a_tool_completes_the_task_when_the_goal_is_met(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, judge = await started(db, admin_db, tmp_path, [judged("met")], [reply])
    finished = await rig.task(task.id)
    assert finished.status == TaskStatus.COMPLETED and finished.goal == ""
    assert finished.result["summary"] == "I have opened the Wi-Fi settings."  # type: ignore[index]
    assert judge.asked[0]["screenshots"]  # it judged the phone, not the agent's word


async def test_a_goal_that_can_never_be_met_is_cleared_and_the_user_is_asked(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, _ = await started(
        db, admin_db, tmp_path, [judged("impossible", "the phone has no Wi-Fi")], [reply]
    )
    run = await rig.root_run(task.id)
    assert (run.status, run.wait_reason) == (RunStatus.WAITING, WaitReason.USER_INPUT)
    assert "the phone has no Wi-Fi" in str(run.wait_detail)
    assert (await rig.task(task.id)).goal == ""
    assert "goal.impossible" in await rig.event_types(task.id)


async def test_an_agent_that_keeps_ending_its_turn_without_getting_anywhere_stops_for_the_user(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, judge = await started(
        db,
        admin_db,
        tmp_path,
        [judged("not_met", "nothing changed")],
        [reply] * (rules.GOAL_STALL + 2),
    )
    run = await rig.root_run(task.id)
    assert (run.status, run.wait_reason) == (RunStatus.WAITING, WaitReason.USER_INPUT)
    assert len(judge.asked) == rules.GOAL_STALL
    assert (await rig.task(task.id)).goal  # still set: the user decides


async def test_a_goal_the_user_paused_is_not_judged(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    judge = FakeJudge([judged("met")])
    rig = Rig(db, tenant, tmp_path, judge=judge)
    await rig.connect([device_info(0)])
    rig.model.script = [reply]
    task = await rig.create_task(GOAL)
    async with rig.db.tenant(tenant.id) as conn:
        await lifecycle.add_input(conn, task_id=task.id, user_id=tenant.user_id, text="/goal pause")
    await rig.settle()
    assert judge.asked == []
    assert (await rig.task(task.id)).goal == GOAL.removeprefix("/goal ")


async def test_a_task_without_a_goal_is_not_judged_when_the_agent_replies(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    judge = FakeJudge([judged("met")])
    rig = Rig(db, tenant, tmp_path, judge=judge)
    await rig.connect([device_info(0)])
    rig.model.script = [reply]
    task = await rig.create_task("Open Wi-Fi settings")
    await rig.settle()
    assert judge.asked == []
    assert (await rig.root_run(task.id)).wait_reason == WaitReason.USER_INPUT


async def test_a_task_without_a_goal_finishes_on_the_agents_word_unjudged(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    judge = FakeJudge([judged("not_met", "never asked")])
    rig = Rig(db, tenant, tmp_path, judge=judge)
    await rig.connect([device_info(0)])
    rig.model.script = [complete]
    task = await rig.create_task("Open Wi-Fi settings")
    await rig.settle()
    assert judge.asked == []
    (finished,) = [o for o in await rig.outputs(task.id) if o["name"] == "complete_task"]
    assert finished["status"] == "accepted"
    done = await rig.task(task.id)
    assert done.status == TaskStatus.COMPLETED
    assert set(done.result or {}) == {"summary", "evidence_observation_ids", "completed_at"}


async def test_the_judge_is_told_what_the_phone_reported_with_a_screenshot(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    judge = FakeJudge([judged("met")])
    rig = Rig(db, tenant, tmp_path, judge=judge)
    await rig.connect([device_info(0) | {"capabilities": ["tap", "swipe", "type_text"]}])
    device = rig.tenant.device_ids[0]
    rig.gateway.sliders = [{"name": "Font size", "value": "50%", "x": 540.0, "y": 1840.0}]
    rig.gateway.field_text = "Ann"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: [
            call(
                "type_text",
                device_id=device,
                observation_id=last_screenshot(r),
                text="Ann",
                field_x=540,
                field_y=900,
                target="Name field",
                expected_effect="The name is in the field",
                effects=[],
            )
        ],
        lambda r: [
            call(
                "complete_task",
                summary="font size 50%, name Ann",
                evidence_observation_ids=[last_screenshot(r)],
            )
        ],
    ]
    task = await rig.create_task("/goal The name field holds Ann")
    await rig.settle()
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    (shot,) = judge.asked[0]["screenshots"]
    assert shot.readings == ["slider 'Font size': 50%", "the field typed into held: 'Ann'"]


def test_the_goal_verdict_is_read_from_the_judges_answer() -> None:
    answer = [
        {
            "type": "function_call",
            "name": "report_goal",
            "arguments": '{"verdict": "not_met", "reason": "switch off"}',
        }
    ]
    assert parse_verdict(answer) == GoalVerdict("not_met", "switch off")
    assert parse_verdict([{**answer[0], "arguments": "{}"}]) is None


@pytest.mark.parametrize("verdict", ["maybe", ""])
def test_an_unknown_goal_verdict_is_no_verdict(verdict: str) -> None:
    arguments = f'{{"verdict": "{verdict}", "reason": "x"}}'
    answer = [{"type": "function_call", "name": "report_goal", "arguments": arguments}]
    assert parse_verdict(answer) is None
