"""End-to-end behaviour of the cloud: model -> admission -> hub -> Gateway -> results.

Each test states one safety property of the Harness.
"""

import re
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from witx.api import views
from witx.config import Limits
from witx.domain import rules
from witx.domain.model import (
    ActionStatus,
    Effect,
    Lease,
    PermissionMode,
    RiskVerdict,
    RunStatus,
    Task,
    TaskStatus,
    WaitReason,
)
from witx.domain.rules import Rejected
from witx.harness.risk import describe_input
from witx.hub import Session
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.store import devices, identity, tasks
from witx.store import schedules as schedule_store
from witx.store.db import Database, execute
from witx_contracts import gateway as wire

from tests.cloud.rig import SCREEN, Rig, last_screenshot
from tests.cloud.support import RISK_USAGE, call, device_info, now, provision, say


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


# Terms for whichever message or payment a test's tap sends (only the needed ones count).
TERMS = {
    "recipient": "Alice",
    "body": "See you at ten",
    "payee": "Corner Shop",
    "amount": "42.50",
    "currency": "CNY",
}


def tap(request, device, **kw):  # type: ignore[no-untyped-def]
    return [
        call(
            "tap",
            device_id=device,
            observation_id=kw.pop("observation_id", None) or last_screenshot(request),
            x=kw.pop("x", 540),
            y=kw.pop("y", 1200),
            target="Wi-Fi row",
            expected_effect="Wi-Fi settings open",
            effects=kw.pop("effects", []),
            intent=kw.pop("intent", TERMS),
            notes=kw.pop("notes", None),
            changes=kw.pop("changes", None),
        )
    ]


def complete(request):  # type: ignore[no-untyped-def]
    return [
        call(
            "complete_task",
            summary="Wi-Fi settings are open",
            evidence_observation_ids=[last_screenshot(request)],
        )
    ]


async def test_observe_act_verify_complete(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [say("Looking at the screen."), call("observe", device_id=device)],
        lambda r: tap(r, device),
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()

    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.DONE and action.effect == Effect.UNVERIFIED
    # Every tool call was answered, and the model saw the follow-up screenshot.
    outputs = await rig.outputs(task.id)
    assert [o["name"] for o in outputs] == ["observe", "tap", "complete_task"]
    assert outputs[1]["observation"]["status"] == "ok"
    assert "input_image" in str(rig.model.requests[1].input)
    assert "agent.message" in await rig.event_types(task.id)


async def test_what_an_input_changes_is_kept_with_it_in_a_few_words(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    wordy = "plant cherries on every free plot of the farm, then water them"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, effects=["change_data"], changes="  种植樱桃 "),
        lambda r: tap(r, device, effects=["change_data"], changes=wordy),
        lambda r: tap(r, device),
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()

    planted, wordy_one, plain = await rig.actions(task.id)
    assert planted.changes == "种植樱桃"
    assert wordy_one.changes == wordy[:40]
    assert plain.changes is None
    assert views.action_view(planted, None, now()).changes == "种植樱桃"


async def test_a_run_s_first_turn_already_shows_each_of_its_phones(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [lambda r: tap(r, device), complete]  # no observe first
    task = await rig.create_task()
    await rig.settle()
    first = str(rig.model.requests[0].input)
    assert "taken as you start" in first and first.count("image_url") == 1
    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.DONE
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED


async def test_a_run_starts_even_when_its_first_screenshot_fails(rig: Rig) -> None:
    rig.gateway.capture_error = "appium_error"
    rig.model.script = [complete]
    await rig.create_task()
    await rig.settle()
    first = str(rig.model.requests[0].input)
    assert "taken as you start failed" in first and "image_url" not in first


async def test_each_request_attaches_only_the_newest_screenshot_and_names_the_others(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    look = [call("observe", device_id=device)]
    rig.model.script = [look, look, look, complete]
    await rig.create_task()
    await rig.settle()

    sent = str(rig.model.requests[3].input)
    assert sent.count("image_url") == 1
    assert sent.count("not attached]") == 3  # the one taken as the run started, and two more


async def test_what_the_agent_noted_stays_in_view_after_its_screenshot_is_dropped(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, notes="Storage: 6.6 GB used"),
        [call("observe", device_id=device)],
        complete,
    ]
    await rig.create_task()
    await rig.settle()

    later = str(rig.model.requests[3].input)
    assert "Storage: 6.6 GB used" in later
    assert later.count("image_url") == 1


def first_screenshot(request):  # type: ignore[no-untyped-def]
    return re.findall(r"Screenshot ([0-9a-f-]{36}):", str(request.input))[0]


async def test_the_model_can_look_at_an_earlier_screenshot_again_for_one_turn(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    look = [call("observe", device_id=device)]
    rig.model.script = [
        look,
        look,
        lambda r: [call("view_screenshot", observation_id=first_screenshot(r))],
        look,
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()

    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    viewing = str(rig.model.requests[3].input)
    assert viewing.count("image_url") == 2  # the newest screenshot and the one asked for
    assert "Earlier screenshot" in viewing
    assert str(rig.model.requests[4].input).count("image_url") == 1


async def test_only_this_task_s_screenshots_can_be_looked_at_again(rig: Rig) -> None:
    rig.model.script = [
        [call("view_screenshot", observation_id=str(uuid4()))],
        [call("complete_task", summary="nothing to see", evidence_observation_ids=[])],
    ]
    task = await rig.create_task()
    await rig.settle()

    (output, _) = await rig.outputs(task.id)
    assert output["status"] == "rejected" and output["code"] == "unknown_screenshot"


async def test_a_turn_that_searched_the_web_goes_on_and_shows_what_it_searched(rig: Rig) -> None:
    searched = {
        "type": "web_search_call",
        "id": "ws_1",
        "status": "completed",
        "action": {"type": "search", "query": "999感冒灵 价格"},
    }
    turn_on = [call("use_web_search", reason="the user asks for a price online")]
    rig.model.script = [turn_on, [searched], complete_plain]
    task = await rig.create_task("上网查一下 999感冒灵 多少钱", device_ids=[])
    await rig.settle()

    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED  # not waiting for the user
    events = await rig.event_types(task.id)
    assert "agent.web_search_on" in events and "agent.web_search" in events
    # Off until the agent turned it on for this task.
    assert [r.web_search for r in rig.model.requests] == [False, True, True]


async def test_an_agent_that_only_keeps_searching_stops_and_asks(rig: Rig) -> None:
    searched = {
        "type": "web_search_call",
        "id": "ws_1",
        "status": "completed",
        "action": {"type": "search", "query": "price"},
    }
    turn_on = [call("use_web_search", reason="needs a price")]
    rig.model.script = [turn_on, [searched], [searched], [searched]]
    task = await rig.create_task("How much is it?", device_ids=[])
    await rig.settle()

    assert len(rig.model.requests) == 4  # no fourth search at the user's expense
    assert "agent.question" in await rig.event_types(task.id)
    # After a turn that only searched, the agent is told its results were not kept.
    assert "search results are not kept" in str(rig.model.requests[2].input)


async def test_a_task_starts_without_web_search(rig: Rig) -> None:
    rig.model.script = [complete_plain]
    await rig.create_task("Open Settings", device_ids=[])
    await rig.settle()
    assert rig.model.requests[0].web_search is False


def complete_plain(request):  # type: ignore[no-untyped-def]
    return [call("complete_task", summary="about ¥20", evidence_observation_ids=[])]


async def test_the_fast_risk_check_is_used_only_when_an_owner_chose_it(
    rig: Rig, admin_db: Database
) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device), complete]
    await rig.create_task()
    await rig.settle()
    async with admin_db.system() as conn:
        await identity.set_fast_risk_check(conn, rig.tenant.id, True)
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device), complete]
    await rig.create_task("Open Bluetooth settings")
    await rig.settle()
    assert [c["fast"] for c in rig.risk.calls] == [False, True]


async def test_a_message_after_the_task_finished_is_answered_as_the_newest_after_what_was_said(
    rig: Rig,
) -> None:
    rig.model.script = [complete_plain]
    task = await rig.create_task("Reply 'ok' to Mint on WeChat", device_ids=[])
    await rig.settle()
    for text in ("How much is the iPhone 18 Pro?", "Which model are you?"):
        async with rig.db.tenant(rig.tenant.id) as conn:
            await lifecycle.add_input(conn, task_id=task.id, user_id=rig.tenant.user_id, text=text)
        rig.model.script = [complete_plain]
        await rig.settle()

    intro = str(rig.model.requests[-1].input[0])
    said, _, now = intro.partition("What the user wrote after your last answer")
    # In order: each message before the answer to it, the first request among them.
    assert said.index("Reply 'ok' to Mint") < said.index("iPhone 18 Pro") < said.rindex("about ¥20")
    assert "The user's request" not in intro
    assert "Which model are you?" in now and "iPhone 18 Pro" not in now


async def test_each_finished_round_keeps_its_result_after_the_task_goes_on(rig: Rig) -> None:
    rig.model.script = [complete_plain]
    task = await rig.create_task("How much is a coffee?", device_ids=[])
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="And a tea?"
        )
        resumed = await tasks.get(conn, task.id)
        assert resumed is not None
        going = await views.task_detail(conn, resumed)
    assert going.result is None  # the task works again
    assert [r.result.summary for r in going.runs if r.result] == ["about ¥20"]
    rig.model.script = [complete_plain]
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        done = await views.task_detail(conn, await rig.task(task.id))
    assert len([r for r in done.runs if r.result]) == 2 and done.result is not None


async def test_a_task_that_reached_the_step_limit_shows_why_it_failed(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path, Limits(max_steps_per_run=1))
    await rig.connect([device_info(0)])
    rig.model.script = [[call("observe", device_id=tenant.device_ids[0])]]
    task = await rig.create_task("Open the calculator")
    await rig.settle()
    async with rig.db.tenant(tenant.id) as conn:
        detail = await views.task_detail(conn, await rig.task(task.id))
    assert detail.status == "failed"
    assert detail.result is not None and detail.result.failure == "step_limit"
    assert "step limit (1 steps)" in detail.result.summary
    (run,) = detail.runs
    assert run.status == "failed" and run.result == detail.result


async def test_a_changed_goal_keeps_the_task_s_first_title(rig: Rig) -> None:
    task = await rig.create_task("你好", device_ids=[])
    async with rig.db.tenant(rig.tenant.id) as conn:
        await tasks.set_title(conn, task.id, "打招呼")
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="/goal 查明天上海的天气"
        )
        updated = await tasks.get(conn, task.id)
    assert updated is not None and updated.title == "打招呼"


async def test_a_goal_message_from_the_user_sets_the_goal_the_agent_works_on(rig: Rig) -> None:
    rig.model.script = [complete_plain, complete_plain]
    task = await rig.create_task("Open Wi-Fi settings", device_ids=[])
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="/goal Check the battery"
        )
    await rig.settle()
    assert (await rig.task(task.id)).goal == "Check the battery"
    assert "task.goal" in await rig.event_types(task.id)
    assert "Goal (work until it is met): Check the battery" in str(rig.model.requests[-1].input)


async def test_a_plain_request_has_no_goal(rig: Rig) -> None:
    rig.model.script = [complete_plain]
    task = await rig.create_task("Open Wi-Fi settings", device_ids=[])
    await rig.settle()
    assert task.goal == "" and task.request == "Open Wi-Fi settings"
    sent = str(rig.model.requests[0].input)
    assert "The user's request: Open Wi-Fi settings" in sent and "Goal (" not in sent


async def test_the_agent_gives_a_long_task_a_goal_once(rig: Rig) -> None:
    goal = "All five shops compared, cheapest one named"
    rig.model.script = [
        [call("set_goal", goal=goal)],
        [call("set_goal", goal="Something else")],
        complete_plain,
    ]
    task = await rig.create_task("Compare coffee prices in five shops", device_ids=[])
    await rig.settle()
    assert (await rig.task(task.id)).goal == goal
    outputs = await rig.outputs(task.id)
    assert outputs[1]["code"] == "goal_exists"  # not over a goal the task has


async def test_only_the_user_sets_the_goal(rig: Rig) -> None:
    rig.model.script = [complete_plain]
    task = await rig.create_task("/goal Open Wi-Fi settings", device_ids=[])
    await rig.settle()
    assert task.goal == "Open Wi-Fi settings"  # "/goal" as the first message too
    offered = {t["name"] for t in rig.model.requests[0].tools}
    assert "update_goal" not in offered and "complete_task" in offered


async def test_the_agent_is_told_the_start_time_in_the_user_s_time_zone(rig: Rig) -> None:
    rig.model.script = [complete_plain]
    task = await rig.create_task("What day is tomorrow?", device_ids=[], timezone="Asia/Shanghai")
    await rig.settle()
    local = task.created_at.astimezone(ZoneInfo("Asia/Shanghai"))
    sent = str(rig.model.requests[0].input)
    assert f"{local:%Y-%m-%d %H:%M} ({local:%A})" in sent and "Asia/Shanghai" in sent


async def test_a_schedule_the_user_asks_for_is_proposed_and_stays_off(rig: Rig) -> None:
    proposal = call(
        "propose_schedule",
        goal="Check the battery",
        repeat="daily",
        time_of_day="08:00",
        weekdays=[],
        month_day=None,
        run_on=None,
    )
    device = rig.tenant.device_ids[0]
    rig.model.script = [[proposal], [call("observe", device_id=device)], complete]
    task = await rig.create_task("Every morning at 8, check the battery")
    await rig.settle()

    async with rig.db.tenant(rig.tenant.id) as conn:
        (schedule,) = await schedule_store.list_all(conn)
    assert schedule.enabled is False and schedule.next_run_at is None  # the user turns it on
    assert schedule.device_ids == rig.tenant.device_ids  # the task's phones
    assert schedule.last_outcome == "proposed"
    assert "schedule.proposed" in await rig.event_types(task.id)


async def test_a_schedule_proposal_with_a_bad_time_is_refused(rig: Rig) -> None:
    proposal = call(
        "propose_schedule",
        goal="Check",
        repeat="daily",
        time_of_day="8 am",
        weekdays=[],
        month_day=None,
        run_on=None,
    )
    rig.model.script = [[proposal], complete_plain]
    task = await rig.create_task("Every morning", device_ids=[])
    await rig.settle()
    (output, _) = await rig.outputs(task.id)
    assert output["status"] == "rejected"


async def test_the_risk_check_is_kept_for_audit_and_counted_in_usage(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device), complete]
    task = await rig.create_task()
    await rig.settle()

    (record,) = await rig.risk_calls(task.id)
    assert record["usage"] == RISK_USAGE
    usage = await rig.usage(task.id)
    assert usage["model_calls"] == 4  # three agent steps and one risk check
    assert (usage["input_tokens"], usage["output_tokens"]) == (700, 20)


async def test_a_picker_value_reaches_the_gateway_as_one_input(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    picker = ["tap", "swipe", "set_picker", "home", "launch_app"]
    await rig.connect([device_info(0) | {"platform": "ios", "capabilities": picker}])
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: [
            call(
                "set_picker",
                device_id=device,
                observation_id=last_screenshot(r),
                x=300,
                y=1200,
                value="10",
                target="Hour wheel",
                expected_effect="The hour shows 10",
                effects=[],
            )
        ],
        complete,
    ]
    task = await rig.create_task("Set the alarm hour to 10")
    await rig.settle()

    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    (sent,) = rig.gateway.actions
    assert sent.input.model_dump() == {"kind": "set_picker", "x": 300.0, "y": 1200.0, "value": "10"}


async def test_sensitive_input_waits_for_approval_and_is_bound_to_its_parameters(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, effects=["payment"]),
        complete,
    ]
    task = await rig.create_task("Buy the item in the cart")
    await rig.settle()

    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.AWAITING_APPROVAL
    assert rig.gateway.actions == []  # nothing reached the device
    run = await rig.root_run(task.id)
    assert run.status == RunStatus.WAITING and run.wait_reason == WaitReason.APPROVAL

    async with rig.db.tenant(rig.tenant.id) as conn:
        await exec_service.decide(
            conn, action_id=action.id, decision="approve", user_id=rig.tenant.user_id, now=now()
        )
    await rig.settle()
    assert [a.action_id for a in rig.gateway.actions] == [action.id]
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED


async def test_denied_input_is_never_sent(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, effects=["send_message"]),
        [call("ask_user", question="Should I try something else?")],
    ]
    task = await rig.create_task("Send the message", permission_mode=PermissionMode.ASK)
    await rig.settle()
    (action,) = await rig.actions(task.id)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await exec_service.decide(
            conn, action_id=action.id, decision="deny", user_id=rig.tenant.user_id, now=now()
        )
    await rig.settle()
    assert rig.gateway.actions == []
    assert (await rig.outputs(task.id))[1]["status"] == "denied"
    assert (await rig.root_run(task.id)).wait_reason == WaitReason.USER_INPUT


async def test_unknown_effect_blocks_input_until_reconciled(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.outcome = "unknown"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device),
        lambda r: tap(r, device),  # tries again: must be refused
        [call("ask_user", question="Did the tap happen?")],
    ]
    task = await rig.create_task()
    await rig.settle()

    (first,) = await rig.actions(task.id)
    assert first.effect == Effect.UNKNOWN
    outputs = await rig.outputs(task.id)
    assert outputs[2]["status"] == "rejected" and outputs[2]["code"] == "unknown_effect_pending"
    assert len(rig.gateway.actions) == 1

    # Reconciling needs a screenshot taken after the action.
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(exec_service.Rejected, match="observation_too_old"):
            await exec_service.reconcile(
                conn,
                action_id=first.id,
                effect="no_effect",
                observation_id=first.observation_id,
                user_id=rig.tenant.user_id,  # type: ignore[arg-type]
            )
        await exec_service.reconcile(
            conn,
            action_id=first.id,
            effect="no_effect",
            observation_id=first.post_observation_id,
            user_id=rig.tenant.user_id,
        )
    rig.gateway.outcome = "returned"
    rig.model.script = [lambda r: tap(r, device), complete]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="It did not."
        )
    await rig.settle()
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    assert len(rig.gateway.actions) == 2
    _, retry = await rig.actions(task.id)
    assert retry.retry_of == first.id  # a new action ID, tied to the one it tries again


async def test_coordinates_must_refer_to_the_newest_screenshot(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    first_seen: dict[str, str] = {}

    def remember_and_observe(r):  # type: ignore[no-untyped-def]
        first_seen["id"] = last_screenshot(r)
        return [call("observe", device_id=device)]

    rig.model.script = [
        [call("observe", device_id=device)],
        remember_and_observe,
        lambda r: tap(r, device, observation_id=first_seen["id"]),
        lambda r: tap(r, device, x=5000),
        [call("ask_user", question="?")],
    ]
    task = await rig.create_task()
    await rig.settle()
    outputs = await rig.outputs(task.id)
    assert outputs[2]["code"] == "stale_observation"
    assert outputs[3]["code"] == "invalid_arguments" or outputs[3]["code"] == "out_of_bounds"
    assert rig.gateway.actions == []


async def test_one_input_per_turn(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device) + tap(r, device, x=10),
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()
    outputs = await rig.outputs(task.id)
    assert outputs[2]["code"] == "one_input_per_turn"
    assert len(rig.gateway.actions) == 1


async def test_completion_needs_fresh_evidence(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    seen: dict[str, str] = {}

    def tap_and_remember(r):  # type: ignore[no-untyped-def]
        seen["before"] = last_screenshot(r)
        return tap(r, device)

    rig.model.script = [
        [call("observe", device_id=device)],
        tap_and_remember,
        lambda r: [
            call("complete_task", summary="done", evidence_observation_ids=[seen["before"]])
        ],
        lambda r: [call("complete_task", summary="done", evidence_observation_ids=[])],
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()
    outputs = await rig.outputs(task.id)
    assert (
        outputs[2]["status"] == "rejected"
        and "taken after your last input" in outputs[2]["gaps"][0]
    )
    assert outputs[3]["status"] == "rejected"
    assert outputs[4]["status"] == "accepted"
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED


async def test_a_reply_without_tools_waits_for_the_user(rig: Rig) -> None:
    rig.model.script = [[say("Which network should I join?")]]
    task = await rig.create_task()
    await rig.settle()
    run = await rig.root_run(task.id)
    assert run.status == RunStatus.WAITING and run.wait_reason == WaitReason.USER_INPUT
    assert (await rig.task(task.id)).status == TaskStatus.ACTIVE

    rig.model.script = [[say("Thanks.")]]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="Use Home-5G"
        )
    await rig.settle()
    assert "Use Home-5G" in str(rig.model.requests[-1].input)


async def test_stop_is_a_barrier(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.hold_actions = True
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device)]
    task = await rig.create_task()
    await rig.settle()
    assert len(rig.gateway.held) == 1  # an input is in flight

    async with rig.db.tenant(rig.tenant.id) as conn:
        stopping = await lifecycle.stop(conn, task.id)
    assert stopping.status == TaskStatus.STOPPING
    await rig.gateway_round()
    assert len(rig.gateway.stops) == 1
    assert (await rig.task(task.id)).status == TaskStatus.STOPPING  # not acknowledged yet

    # The in-flight input finishes, then the Gateway confirms the barrier.
    held = rig.gateway.held.pop()
    await rig.finish_action(held, "returned")
    stop = rig.gateway.stops[0]
    await rig.handle(wire.StopAck(stop_id=stop.stop_id, device_id=stop.device_id))
    assert (await rig.task(task.id)).status == TaskStatus.STOPPED
    (action,) = await rig.actions(task.id)
    assert action.effect == Effect.UNVERIFIED  # the late result is still recorded


async def pause(rig: Rig, task_id: UUID) -> Task:
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await lifecycle.pause(conn, task_id)


async def resume(rig: Rig, task_id: UUID) -> Task:
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await lifecycle.resume(conn, task_id)


async def lease_of(rig: Rig, device_id: UUID) -> Lease | None:
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await devices.lease(conn, device_id)


async def paused_after_observing(rig: Rig) -> Task:
    """A task that observed once, then was paused and confirmed by the Gateway."""
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)]]
    task = await rig.create_task()
    await rig.worker.run_once()  # the screenshot taken as the run starts
    await rig.gateway_round()
    await rig.worker.run_once()  # observe
    await rig.gateway_round()  # the screenshot arrives
    await pause(rig, task.id)
    await rig.gateway_round()  # stop barrier sent and confirmed
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED
    return task


async def test_a_pause_completes_after_the_barrier_and_keeps_the_device(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.hold_actions = True
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device)]
    task = await rig.create_task()
    await rig.settle()
    assert len(rig.gateway.held) == 1  # an input is in flight

    assert (await pause(rig, task.id)).status == TaskStatus.PAUSING
    await rig.gateway_round()
    assert len(rig.gateway.stops) == 1
    assert (await rig.task(task.id)).status == TaskStatus.PAUSING  # not confirmed yet

    await rig.finish_action(rig.gateway.held.pop(), "returned")
    stop = rig.gateway.stops[0]
    await rig.handle(wire.StopAck(stop_id=stop.stop_id, device_id=stop.device_id))
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED
    lease = await lease_of(rig, device)
    assert lease is not None and lease.task_id == task.id  # the device stays with the task
    run = await rig.root_run(task.id)
    assert run.status == RunStatus.WAITING and run.wait_reason == WaitReason.PAUSED
    calls = len(rig.model.requests)
    await rig.settle()
    assert len(rig.model.requests) == calls  # no model calls while paused


async def test_after_resume_input_needs_a_new_screenshot(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    task = await paused_after_observing(rig)
    epoch = rig.gateway.epochs[device]
    rig.model.script = [
        lambda r: tap(r, device),  # still refers to the screenshot from before the pause
        [call("observe", device_id=device)],
        lambda r: tap(r, device),
        complete,
    ]
    assert (await resume(rig, task.id)).status == TaskStatus.ACTIVE
    await rig.settle()

    assert rig.gateway.epochs[device] > epoch  # a new lease epoch after the pause
    taps = [o for o in await rig.outputs(task.id) if o["name"] == "tap"]
    assert taps[0]["status"] == "rejected" and taps[0]["code"] == "stale_observation"
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    (action,) = await rig.actions(task.id)
    assert action.epoch > epoch


async def test_a_task_cannot_resume_before_the_pause_is_confirmed(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.hold_actions = True
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device)]
    task = await rig.create_task()
    await rig.settle()
    await pause(rig, task.id)
    await rig.gateway_round()  # the barrier waits for the input in flight
    with pytest.raises(Rejected) as error:
        await resume(rig, task.id)
    assert error.value.code == "task_pausing"


async def test_input_during_a_pause_waits_for_resume(rig: Rig) -> None:
    task = await paused_after_observing(rig)
    calls = len(rig.model.requests)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="also check Wi-Fi"
        )
    await rig.settle()
    assert len(rig.model.requests) == calls
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED
    assert (await rig.root_run(task.id)).wait_reason == WaitReason.PAUSED


async def test_stopping_a_paused_task_releases_its_devices(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    task = await paused_after_observing(rig)
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert (await lifecycle.stop(conn, task.id)).status == TaskStatus.STOPPING
    await rig.gateway_round()  # a new barrier, confirmed
    assert (await rig.task(task.id)).status == TaskStatus.STOPPED
    assert await lease_of(rig, device) is None


async def test_delegated_device_returns_to_the_parent(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [
            call(
                "delegate", subtasks=[{"goal": "Check the battery level", "device_id": str(device)}]
            )
        ],
        # child run:
        [call("observe", device_id=device)],
        complete,
        # parent again: it sees the child's result
        [call("observe", device_id=device)],
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    outputs = await rig.outputs(task.id)
    delegate = next(o for o in outputs if o["name"] == "delegate")
    (child,) = delegate["results"]
    assert child["status"] == "completed" and "Wi-Fi" in child["summary"]


async def test_a_sub_agent_keeps_the_name_it_was_given(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [
            call(
                "delegate",
                subtasks=[
                    {
                        "goal": "Check the battery",
                        "name": "  Battery   Agent ",
                        "device_id": str(device),
                    }
                ],
            )
        ],
        [call("observe", device_id=device)],
        complete,
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        detail = await views.task_detail(conn, await tasks.get(conn, task.id))  # type: ignore[arg-type]
    assert [r.name for r in detail.runs] == [None, "Battery Agent"]


async def test_screen_change_between_observation_and_input_is_detected(rig: Rig) -> None:
    """The Gateway checks the screen state; here we check the cloud sends what it saw."""
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device), complete]
    await rig.create_task()
    await rig.settle()
    (sent,) = rig.gateway.actions
    assert sent.expected.model_dump(exclude={"sliders"}) == SCREEN
    assert sent.input.kind == "tap"


# ---------------------------------------------------------------- permission modes


async def _proposed(rig: Rig, effects: list[str] | None = None, **task_kw) -> "list":  # type: ignore[no-untyped-def]
    """Runs until the model has proposed one tap, without delivering it."""
    device = rig.tenant.device_ids[0]
    rig.model.script = [lambda r: tap(r, device, effects=effects or []), complete]
    task = await rig.create_task(**task_kw)
    await rig.worker.run_once()  # the screenshot taken as the run starts
    await rig.gateway_round()  # capture
    await rig.worker.run_once()  # tap is admitted, not yet delivered
    return [task, *(await rig.actions(task.id))]


async def test_input_the_risk_check_flags_waits_even_if_the_model_declared_nothing(
    rig: Rig,
) -> None:
    rig.risk.verdict = RiskVerdict(sensitive=True, effects=["payment"], reason="Pay button")
    task, action = await _proposed(rig)
    assert action.status == ActionStatus.AWAITING_APPROVAL
    assert action.policy is not None and "risk check: Pay button" in action.policy.reasons
    await rig.settle()
    assert rig.gateway.actions == []
    assert rig.risk.calls[0]["payload"]["kind"] == "tap"
    assert (await rig.root_run(task.id)).wait_reason == WaitReason.APPROVAL


async def test_a_failed_risk_check_holds_input_for_approval(rig: Rig) -> None:
    rig.risk.verdict = RiskVerdict(sensitive=True, effects=[], reason="timeout", failed=True)
    _, action = await _proposed(rig)
    assert action.status == ActionStatus.AWAITING_APPROVAL


async def test_ask_mode_lets_looking_around_run_and_holds_any_change(rig: Rig) -> None:
    _, look = await _proposed(rig, permission_mode=PermissionMode.ASK)
    assert look.status == ActionStatus.QUEUED  # the risk check saw nothing change
    assert rig.risk.calls[0]["payload"]["kind"] == "tap"


async def test_ask_mode_holds_a_change_the_model_did_not_declare(rig: Rig) -> None:
    rig.risk.verdict = RiskVerdict(sensitive=False, effects=["change_data"], reason="saves")
    task, save = await _proposed(rig, permission_mode=PermissionMode.ASK)
    assert save.status == ActionStatus.AWAITING_APPROVAL
    async with rig.db.tenant(rig.tenant.id) as conn:  # the task page shows it
        detail = await views.task_detail(conn, await rig.task(task.id))
    assert detail.actions[0].policy.risk.effects == ["change_data"]  # type: ignore[union-attr]


async def test_full_access_needs_an_owner_to_allow_it(rig: Rig, admin_db: Database) -> None:
    with pytest.raises(Rejected, match="full_access_not_allowed"):
        await rig.create_task(permission_mode=PermissionMode.FULL)
    async with admin_db.system() as conn:
        await identity.set_allow_full_access(conn, rig.tenant.id, True)
    task = await rig.create_task(permission_mode=PermissionMode.FULL)
    assert task.permission_mode == PermissionMode.FULL


async def test_full_access_runs_other_sensitive_input(rig: Rig, admin_db: Database) -> None:
    async with admin_db.system() as conn:
        await identity.set_allow_full_access(conn, rig.tenant.id, True)
    rig.risk.verdict = RiskVerdict(sensitive=True, effects=["send_message"], reason="Send")
    _, message = await _proposed(rig, permission_mode=PermissionMode.FULL)
    assert message.status == ActionStatus.QUEUED


async def test_full_access_still_holds_payments(rig: Rig, admin_db: Database) -> None:
    async with admin_db.system() as conn:
        await identity.set_allow_full_access(conn, rig.tenant.id, True)
    rig.risk.verdict = RiskVerdict(sensitive=True, effects=["payment"], reason="Pay")
    _, payment = await _proposed(rig, permission_mode=PermissionMode.FULL)
    assert payment.status == ActionStatus.AWAITING_APPROVAL


async def test_a_stricter_mode_holds_input_that_was_queued(rig: Rig) -> None:
    rig.risk.verdict = RiskVerdict(sensitive=False, effects=["change_data"], reason="saves")
    task, queued = await _proposed(rig)  # auto lets an ordinary change run
    assert queued.status == ActionStatus.QUEUED
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.set_permission_mode(
            conn, task_id=task.id, mode=PermissionMode.ASK, limits=rig.limits, now=now()
        )
    (held,) = await rig.actions(task.id)
    assert held.status == ActionStatus.AWAITING_APPROVAL
    await rig.gateway_round()
    assert rig.gateway.actions == []


async def test_a_looser_mode_does_not_release_input_waiting_for_approval(
    rig: Rig, admin_db: Database
) -> None:
    async with admin_db.system() as conn:
        await identity.set_allow_full_access(conn, rig.tenant.id, True)
    task, _ = await _proposed(rig, ["change_data"], permission_mode=PermissionMode.ASK)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.set_permission_mode(
            conn, task_id=task.id, mode=PermissionMode.FULL, limits=rig.limits, now=now()
        )
    await rig.gateway_round()
    (still,) = await rig.actions(task.id)
    assert still.status == ActionStatus.AWAITING_APPROVAL and rig.gateway.actions == []


async def test_every_input_in_a_sensitive_app_waits(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "bank", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([{**device_info(0), "sensitive_apps": [SCREEN["foreground_app"]]}])
    _, action = await _proposed(rig)
    assert action.status == ActionStatus.AWAITING_APPROVAL
    assert action.policy is not None and action.policy.app_sensitive


async def test_a_result_delivered_twice_counts_once(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.hold_actions = True
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device)]
    task = await rig.create_task()
    await rig.settle()
    (held,) = rig.gateway.held
    await rig.finish_action(held, "returned")
    await rig.finish_action(held, "unknown")  # a late duplicate saying something else
    (action,) = await rig.actions(task.id)
    assert action.effect == Effect.UNVERIFIED  # the first result stands
    updates = [e for e in await rig.event_types(task.id) if e == "action.updated"]
    assert updates.count("action.updated") == 2  # sent, then done: nothing for the duplicate


async def test_after_a_reconnect_input_in_flight_is_resent_under_the_same_id(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.hold_actions = True
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device)]
    await rig.create_task()
    await rig.settle()
    (held,) = rig.gateway.held
    rig.session = Session(rig.tenant.gateway_id, rig.tenant.id, rig._send)  # a new connection
    await rig.connect([device_info(0)])
    resent = [m for m in await rig.hub.collect(rig.session) if isinstance(m, wire.Action)]
    assert [(a.action_id, a.digest) for a in resent] == [(held.action_id, held.digest)]


async def test_a_screenshot_lost_after_an_input_is_given_up_and_the_agent_looks_again(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path, Limits(observation_timeout=timedelta(seconds=0)))
    await rig.connect([device_info(0)])
    device = tenant.device_ids[0]
    rig.gateway.hold_actions = True
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device)]
    task = await rig.create_task()
    await rig.settle()
    (held,) = rig.gateway.held
    # The input ran, but the screenshot after it never came (the connection dropped).
    await rig.handle(
        wire.ActionResult(action_id=held.action_id, outcome="returned", finished_at=now())
    )
    async with rig.db.tenant(tenant.id) as conn:
        await execute(conn, "UPDATE runs SET next_wake_at = now() WHERE task_id = :t", t=task.id)
    rig.model.script = [[call("observe", device_id=device)], complete]
    await rig.settle()
    outputs = await rig.outputs(task.id)
    assert outputs[1]["effect"] == "unverified" and "observation" not in outputs[1]
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED  # it looked again


async def typing_device(rig: Rig) -> UUID:
    await rig.connect([device_info(0) | {"capabilities": ["tap", "swipe", "type_text"]}])
    return rig.tenant.device_ids[0]


def type_into_field(request, device, replace=False, method="set"):  # type: ignore[no-untyped-def]
    return [
        call(
            "type_text",
            device_id=device,
            observation_id=last_screenshot(request),
            text="13700130810",
            field_x=300,
            field_y=400,
            replace=replace,
            method=method,
            target="Phone number field",
            expected_effect="The number is in the field",
            effects=[],
        )
    ]


async def test_a_field_that_lost_a_typed_character_is_reported_to_the_agent(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.gateway.field_text = "+86 137 0013 810"
    rig.gateway.field_name = "Phone"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device),
        complete,
    ]
    task = await rig.create_task("Save 13700130810 as a contact")
    await rig.settle()
    (typed,) = [o for o in await rig.outputs(task.id) if o["name"] == "type_text"]
    assert typed["field_shows"] == "+86 137 0013 810"
    assert typed["typed_into"] == "Phone"  # which field the text went into
    (action,) = [a for a in await rig.actions(task.id) if a.kind == "type_text"]
    assert action.field_info == {"name": "Phone", "box": [144, 2125, 992, 192]}
    assert "never treat the two as the same" in typed["note"]


async def test_the_point_in_the_field_reaches_the_gateway_with_the_input(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device),
        complete,
    ]
    await rig.create_task("Save 13700130810 as a contact")
    await rig.settle()
    (sent,) = rig.gateway.actions
    assert (sent.input.field_x, sent.input.field_y) == (300, 400)  # type: ignore[union-attr]
    assert sent.input.replace is False  # type: ignore[union-attr]


async def test_text_that_replaces_the_field_reaches_the_gateway_as_one_input(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device, replace=True),
        complete,
    ]
    await rig.create_task("Change the contact's number to 13700130810")
    await rig.settle()
    (sent,) = rig.gateway.actions  # emptying the field is part of the input, not a second one
    assert sent.input.replace is True  # type: ignore[union-attr]


async def test_how_the_text_goes_in_reaches_the_gateway_with_the_input(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device, method="keys"),
        complete,
    ]
    await rig.create_task("Save 13700130810 as a contact")
    await rig.settle()
    (sent,) = rig.gateway.actions
    assert sent.input.method == "keys"  # type: ignore[union-attr]


async def test_a_field_not_read_back_after_text_was_set_is_retried_by_keys(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.gateway.field_read = False  # the box closed when the text was set into it
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device),
        complete,
    ]
    task = await rig.create_task("Comment on the post")
    await rig.settle()
    (typed,) = [o for o in await rig.outputs(task.id) if o["name"] == "type_text"]
    assert "method 'keys'" in typed["note"]


async def test_typing_by_keys_that_stopped_short_says_why_and_what_went_in(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.gateway.type_problem = "no_candidate"
    rig.gateway.field_text = "1370"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device, method="keys"),
        complete,
    ]
    task = await rig.create_task("Save 13700130810 as a contact")
    await rig.settle()
    (typed,) = [o for o in await rig.outputs(task.id) if o["name"] == "type_text"]
    assert (typed["reason"], typed["field_shows"]) == ("no_candidate", "1370")
    assert "no candidate" in typed["note"]


def test_the_risk_check_is_told_the_text_replaces_what_the_field_holds() -> None:
    typed = {"kind": "type_text", "text": "Ann"}
    assert describe_input(typed | {"replace": True}).startswith("replace all text")
    assert describe_input(typed).startswith("type this text")
    assert "by tapping the keyboard" in describe_input(typed | {"method": "keys"})


async def test_the_sliders_in_view_are_listed_in_the_screenshot_result(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.gateway.sliders = [{"name": "Font size", "value": "1.0", "x": 540.0, "y": 1840.0}]
    rig.model.script = [[call("observe", device_id=device)], complete]
    task = await rig.create_task("Put the font size back")
    await rig.settle()
    (seen,) = [o for o in await rig.outputs(task.id) if o["name"] == "observe"]
    assert seen["sliders"] == rig.gateway.sliders


async def test_a_screen_without_sliders_lists_none(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.model.script = [[call("observe", device_id=device)], complete]
    task = await rig.create_task("Look")
    await rig.settle()
    (seen,) = [o for o in await rig.outputs(task.id) if o["name"] == "observe"]
    assert "sliders" not in seen


async def test_a_field_that_reformatted_the_typed_number_is_not_flagged(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.gateway.field_text = "+86 137 0013 0810"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device),
        complete,
    ]
    task = await rig.create_task("Save 13700130810 as a contact")
    await rig.settle()
    (typed,) = [o for o in await rig.outputs(task.id) if o["name"] == "type_text"]
    assert "field_shows" not in typed


def test_text_read_back_ignores_spacing_and_case_but_not_a_changed_word() -> None:
    assert rules.typed_text_shown("Buy milk", "buy  milk!")
    assert rules.typed_text_shown("长任务810", "长任务810")
    assert not rules.typed_text_shown("Meituan", "Meeting")  # autocorrect changed it
    assert not rules.typed_text_shown("长任务810", "长任务81")


async def test_a_text_for_another_field_than_the_one_in_focus_is_refused_and_the_agent_is_told(
    rig: Rig,
) -> None:
    device = await typing_device(rig)
    rig.gateway.wrong_field = True
    rig.gateway.field_name = "Search"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: type_into_field(r, device),
        complete,
    ]
    task = await rig.create_task("Save 13700130810 as a contact")
    await rig.settle()
    (typed,) = [o for o in await rig.outputs(task.id) if o["name"] == "type_text"]
    assert typed["reason"] == "wrong_field" and typed["effect"] == "not_attempted"
    assert typed["focused_field"] == {"name": "Search", "box": [144, 2125, 992, 192]}
    assert "Nothing was typed" in typed["note"]
    assert "typed_into" not in typed  # it went nowhere


async def test_a_field_point_outside_the_screenshot_is_refused_before_it_is_sent(rig: Rig) -> None:
    device = await typing_device(rig)
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: [
            call(
                "type_text",
                device_id=device,
                observation_id=last_screenshot(r),
                text="hello",
                field_x=15000,
                field_y=10,
                target="Name",
                expected_effect="The name is in the field",
                effects=[],
            )
        ],
        complete,
    ]
    task = await rig.create_task("Type hello")
    await rig.settle()
    (typed,) = [o for o in await rig.outputs(task.id) if o["name"] == "type_text"]
    assert (typed["status"], typed["code"]) == ("rejected", "out_of_bounds")
    assert not rig.gateway.actions


async def selecting_device(rig: Rig) -> UUID:
    await rig.connect([device_info(0) | {"capabilities": ["tap", "swipe", "select_option"]}])
    return rig.tenant.device_ids[0]


def select_option_call(request, device, **overrides):  # type: ignore[no-untyped-def]
    args = {
        "device_id": device,
        "observation_id": last_screenshot(request),
        "x": 300,
        "y": 900,
        "value": "China +86",
        "target": "Country code list",
        "expected_effect": "China +86 is chosen",
        "effects": [],
    }
    return [call("select_option", **(args | overrides))]


async def test_the_agent_is_told_which_option_the_phone_tapped(rig: Rig) -> None:
    device = await selecting_device(rig)
    rig.gateway.field_text = "\U0001f1e8\U0001f1f3 China +86"  # what select_option reports back
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: select_option_call(r, device),
        complete,
    ]
    task = await rig.create_task("Choose China +86 as the country code")
    await rig.settle()
    (sent,) = rig.gateway.actions
    assert sent.input.model_dump() == {
        "kind": "select_option",
        "x": 300.0,
        "y": 900.0,
        "value": "China +86",
    }
    (chosen,) = [o for o in await rig.outputs(task.id) if o["name"] == "select_option"]
    assert chosen["selected"] == "\U0001f1e8\U0001f1f3 China +86"
    assert "Check on the new screenshot" in chosen["note"]


async def test_an_option_the_phone_did_not_find_is_reported_without_a_tap(rig: Rig) -> None:
    device = await selecting_device(rig)
    rig.gateway.select_problem = "not_found"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: select_option_call(r, device, value="Atlantis +1"),
        complete,
    ]
    task = await rig.create_task("Choose Atlantis")
    await rig.settle()
    (chosen,) = [o for o in await rig.outputs(task.id) if o["name"] == "select_option"]
    assert chosen["reason"] == "not_found" and "selected" not in chosen
    assert "No option shows exactly that text" in chosen["note"]


async def test_an_option_with_an_effect_is_not_chosen_by_its_text(rig: Rig) -> None:
    device = await selecting_device(rig)
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: select_option_call(r, device, value="Pay now", effects=["payment"]),
        complete,
    ]
    task = await rig.create_task("Pay")
    await rig.settle()
    (chosen,) = [o for o in await rig.outputs(task.id) if o["name"] == "select_option"]
    assert (chosen["status"], chosen["code"]) == ("rejected", "select_not_allowed")
    assert not rig.gateway.actions  # nothing was sent to the phone
