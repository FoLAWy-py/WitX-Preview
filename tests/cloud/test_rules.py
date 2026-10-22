"""The safety rules, without a database."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from witx.domain import rules
from witx.domain.model import (
    EFFECT_DEFINITIONS,
    Action,
    ActionStatus,
    Approval,
    Device,
    Effect,
    Observation,
    ObservationStatus,
    PermissionMode,
    RiskVerdict,
    RunStatus,
    Screen,
    WaitReason,
)
from witx.domain.rules import Rejected

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
DEVICE = Device(
    id=uuid4(),
    tenant_id=uuid4(),
    gateway_id=uuid4(),
    native_id="emulator-5554",
    platform="android",
    name="Pixel",
    capabilities=["tap", "swipe", "launch_app"],
    online=True,
    epoch=3,
    enabled=True,
)
OBS = Observation(
    id=uuid4(),
    tenant_id=DEVICE.tenant_id,
    device_id=DEVICE.id,
    task_id=uuid4(),
    epoch=3,
    status=ObservationStatus.COMPLETE,
    requested_at=NOW - timedelta(seconds=5),
    captured_at=NOW - timedelta(seconds=4),
    screen=Screen(1080, 2400, 1080, 2400, 0, "com.android.settings"),
)
TAP = {"kind": "tap", "x": 100.0, "y": 200.0}


def admit(**overrides):  # type: ignore[no-untyped-def]
    args = dict(
        payload=TAP,
        device=DEVICE,
        observation=OBS,
        latest_observation_id=OBS.id,
        lease_epoch=3,
        last_input_at=None,
        device_busy=False,
        unknown_pending=False,
        now=NOW,
        max_age=timedelta(seconds=120),
    )
    args.update(overrides)
    rules.admit_input(**args)


def test_a_valid_input_is_admitted() -> None:
    admit()


def test_the_agent_cannot_press_a_phones_side_keys_even_where_the_phone_has_them() -> None:
    keys = replace(DEVICE, capabilities=[*DEVICE.capabilities, "volume_up", "volume_down", "lock"])
    for kind in ("volume_up", "volume_down", "lock"):
        with pytest.raises(Rejected, match="unsupported_input"):
            admit(device=keys, payload={"kind": kind})


def test_any_app_on_the_device_may_be_opened() -> None:
    admit(payload={"kind": "launch_app", "app_id": "com.tencent.mm"})


def test_a_run_waits_for_its_devices_only_when_none_of_them_can_be_used() -> None:
    away = replace(DEVICE, name="iPhone", problems=["not_responding"])
    assert rules.device_wait([]) is None  # a task without phones goes on
    assert rules.device_wait([DEVICE, away]) is None  # with the one it can use
    assert (
        rules.device_wait([replace(DEVICE, online=False), away])
        == "waiting for Pixel, iPhone to reconnect"
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"observation": None}, "observation_required"),
        ({"latest_observation_id": uuid4()}, "stale_observation"),
        ({"lease_epoch": 4}, "stale_observation"),
        ({"now": NOW + timedelta(minutes=5)}, "stale_observation"),
        ({"last_input_at": NOW - timedelta(seconds=1)}, "stale_observation"),
        ({"unknown_pending": True}, "unknown_effect_pending"),
        ({"device_busy": True}, "device_busy"),
        ({"payload": {"kind": "tap", "x": 1080.0, "y": 10.0}}, "out_of_bounds"),
        ({"payload": {"kind": "type_text", "text": "hi"}}, "unsupported_input"),
        (
            {"payload": {"kind": "set_picker", "x": 5.0, "y": 5.0, "value": "10"}},
            "unsupported_input",
        ),
        (
            {
                "device": replace(DEVICE, capabilities=["tap", "set_picker"]),
                "payload": {"kind": "set_picker", "x": 5.0, "y": 2400.0, "value": "10"},
            },
            "out_of_bounds",
        ),
        (
            {"payload": {"kind": "select_option", "x": 5.0, "y": 5.0, "value": "China +86"}},
            "unsupported_input",
        ),
        (
            {
                "device": replace(DEVICE, capabilities=["select_option"]),
                "payload": {"kind": "select_option", "x": 5.0, "y": 2400.0, "value": "China +86"},
            },
            "out_of_bounds",
        ),
        ({"device": replace(DEVICE, online=False)}, "device_offline"),
        ({"device": replace(DEVICE, problems=["disconnected"])}, "device_not_ready"),
        ({"device": replace(DEVICE, enabled=False)}, "device_not_enabled"),
        ({"observation": replace(OBS, device_id=uuid4())}, "observation_required"),
    ],
)
def test_inputs_that_break_a_rule_are_rejected(overrides: dict, code: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(Rejected) as error:
        admit(**overrides)
    assert error.value.code == code


def test_declared_effects_must_be_known() -> None:
    assert rules.validate_effects(["payment", "payment"]) == ["payment"]
    with pytest.raises(Rejected):
        rules.validate_effects(["teleport"])


SAFE = RiskVerdict(sensitive=False, effects=[], reason="opens settings")
MESSAGE = RiskVerdict(sensitive=True, effects=["send_message"], reason="sends a message")
PAY = RiskVerdict(sensitive=True, effects=["payment"], reason="confirms payment")
FAILED = RiskVerdict(sensitive=True, effects=[], reason="timeout", failed=True)
CHANGE = RiskVerdict(sensitive=False, effects=["change_data"], reason="saves the note")
ORDER = RiskVerdict(sensitive=True, effects=["purchase"], reason="submits the order")
SHARE = RiskVerdict(sensitive=True, effects=["share_data"], reason="shares the location")
UNNAMED = RiskVerdict(sensitive=True, effects=[], reason="looks risky")

ASK, AUTO, FULL = PermissionMode.ASK, PermissionMode.AUTO, PermissionMode.FULL


@pytest.mark.parametrize(
    ("mode", "declared", "risk", "app_sensitive", "flags", "decision"),
    [
        (ASK, [], SAFE, False, [], "allow"),  # ask: looking around runs
        (ASK, ["change_data"], SAFE, False, [], "ask"),  # the model says it changes data
        (ASK, [], CHANGE, False, [], "ask"),  # only the risk check noticed the change
        (ASK, [], MESSAGE, False, [], "ask"),  # every sensitive input changes something
        (ASK, [], FAILED, False, [], "ask"),  # no verdict cannot rule a change out
        (ASK, [], SAFE, True, [], "ask"),  # sensitive app
        (AUTO, [], SAFE, False, [], "allow"),
        (AUTO, ["change_data"], CHANGE, False, [], "allow"),  # ordinary records run in auto
        (AUTO, ["send_message"], SAFE, False, [], "allow"),  # messages go ahead in auto
        (AUTO, [], MESSAGE, False, ["phone_number"], "allow"),  # with the number typed
        (AUTO, ["purchase"], ORDER, False, [], "allow"),  # an order paid for later
        (AUTO, [], PAY, False, [], "ask"),  # money always waits
        (AUTO, ["delete_data"], SAFE, False, [], "ask"),  # cannot be taken back
        (AUTO, [], SHARE, False, [], "ask"),
        (AUTO, [], SAFE, True, [], "ask"),  # sensitive app
        (AUTO, [], SAFE, False, ["card_number"], "ask"),  # money typed
        (AUTO, [], FAILED, False, [], "ask"),  # no verdict: fail closed
        (AUTO, [], UNNAMED, False, [], "ask"),  # sensitive, but no effect named
        (FULL, [], MESSAGE, True, ["email"], "allow"),  # full: only payments wait
        (FULL, [], PAY, False, [], "ask"),
        (FULL, ["purchase"], ORDER, False, [], "allow"),
        (FULL, [], FAILED, False, [], "ask"),  # cannot rule a payment out
    ],
)
def test_policy_decides_by_mode_and_signals(  # type: ignore[no-untyped-def]
    mode, declared, risk, app_sensitive, flags, decision
) -> None:
    policy = rules.decide_policy(
        mode=mode,
        kind="tap",
        declared_effects=declared,
        risk=risk,
        app_sensitive=app_sensitive,
        text_flags=flags,
    )
    assert policy.decision == decision
    assert policy.reasons or decision == "allow"


def test_moving_turning_the_view_menus_and_unconfirmed_choices_are_not_change_data() -> None:
    # The agent and the risk check read this one definition: what only moves or
    # looks around is not a change; confirming a purchase, a plant or a save is.
    change, nothing = EFFECT_DEFINITIONS["change_data"].split(". ", 1)
    for confirmed in ("saves", "a form it submits", "buying with in-game coins", "planting"):
        assert confirmed in change
    for looking in (
        "moving around in a game or app",
        "turning the view",
        "opening or closing a menu, panel or dialog",
        "selecting something without confirming it",
    ):
        assert looking in nothing and "change nothing" in nothing


def test_an_unassessed_tap_or_picker_value_is_never_assumed_harmless() -> None:
    for mode, kind in [(AUTO, "tap"), (FULL, "tap"), (AUTO, "set_picker"), (FULL, "set_picker")]:
        policy = rules.decide_policy(
            mode=mode,
            kind=kind,
            declared_effects=[],
            risk=None,
            app_sensitive=False,
            text_flags=[],
        )
        assert policy.decision == "ask"
    back = rules.decide_policy(
        mode=FULL,
        kind="back",
        declared_effects=[],
        risk=None,
        app_sensitive=False,
        text_flags=[],
    )
    assert back.decision == "allow"


def test_personal_data_in_typed_text_or_picker_values_is_flagged() -> None:
    assert rules.text_flags({"kind": "type_text", "text": "call +65 9123 4567"}) == ["phone_number"]
    assert rules.text_flags({"kind": "type_text", "text": "a@b.com"}) == ["email"]
    assert rules.text_flags({"kind": "type_text", "text": "weather today"}) == []
    assert rules.text_flags({"kind": "tap", "x": 1, "y": 2}) == []
    picker = {"kind": "set_picker", "x": 1, "y": 2, "value": "\u00a5 500"}
    assert rules.text_flags(picker) == ["money"]


def test_a_stricter_mode_reapplies_the_same_signals() -> None:
    policy = rules.decide_policy(
        mode=FULL,
        kind="tap",
        declared_effects=[],
        risk=SHARE,
        app_sensitive=False,
        text_flags=[],
    )
    assert policy.decision == "allow"
    assert rules.reapply_policy(policy, AUTO, "tap").decision == "ask"


def test_digest_binds_every_parameter() -> None:
    action_id, device_id = uuid4(), uuid4()
    base = rules.action_digest(action_id, device_id, 1, TAP)
    assert base == rules.action_digest(action_id, device_id, 1, dict(TAP))
    assert base != rules.action_digest(action_id, device_id, 1, {**TAP, "x": 101.0})
    assert base != rules.action_digest(action_id, device_id, 2, TAP)
    assert base != rules.action_digest(uuid4(), device_id, 1, TAP)


def _action(**overrides) -> Action:  # type: ignore[no-untyped-def]
    action = Action(
        id=uuid4(),
        tenant_id=DEVICE.tenant_id,
        task_id=uuid4(),
        run_id=uuid4(),
        device_id=DEVICE.id,
        tool_call_id=uuid4(),
        epoch=3,
        control_revision=1,
        observation_id=OBS.id,
        kind="tap",
        input=TAP,
        target="x",
        expected_effect="y",
        effects=["purchase"],
        digest="d" * 64,
        status=ActionStatus.QUEUED,
        effect=Effect.NOT_ATTEMPTED,
        reason=None,
        post_observation_id=uuid4(),
        created_at=NOW,
    )
    return replace(action, **overrides)


def _approval(**overrides) -> Approval:  # type: ignore[no-untyped-def]
    approval = Approval(
        action_id=uuid4(),
        tenant_id=DEVICE.tenant_id,
        digest="d" * 64,
        requested_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        decision="approve",
        decided_at=NOW,
    )
    return replace(approval, **overrides)


def test_delivery_rechecks_lease_revision_and_approval() -> None:
    ok = {"approval": _approval(), "lease_epoch": 3, "control_revision": 1, "now": NOW}
    rules.check_deliverable(_action(), **ok)
    cases = [
        ({"lease_epoch": 4}, "lease_changed"),
        ({"control_revision": 2}, "task_changed"),
        ({"approval": None}, "approval_invalid"),
        ({"approval": _approval(decision=None)}, "approval_invalid"),
        ({"approval": _approval(used_at=NOW)}, "approval_invalid"),
        ({"now": NOW + timedelta(hours=1)}, "approval_invalid"),
        ({"approval": _approval(digest="e" * 64)}, "approval_mismatch"),
    ]
    for override, code in cases:
        with pytest.raises(Rejected) as error:
            rules.check_deliverable(_action(), **{**ok, **override})
        assert error.value.code == code


def test_gateway_outcomes_map_to_effects() -> None:
    assert rules.result_effect("rejected", "tap") == Effect.NOT_ATTEMPTED
    assert rules.result_effect("returned", "tap") == Effect.UNVERIFIED
    assert rules.result_effect("unknown", "tap") == Effect.UNKNOWN
    assert rules.result_effect("returned", "find_app") == Effect.NO_EFFECT  # it only reads


def test_reconcile_needs_a_later_screenshot_of_the_same_device() -> None:
    action = _action(status=ActionStatus.DONE, effect=Effect.UNKNOWN, finished_at=NOW)
    later = replace(OBS, captured_at=NOW + timedelta(seconds=1))
    rules.check_reconcile(action, later)
    with pytest.raises(Rejected, match="observation_too_old"):
        rules.check_reconcile(action, OBS)
    with pytest.raises(Rejected, match="wrong_device"):
        rules.check_reconcile(action, replace(later, device_id=uuid4()))
    with pytest.raises(Rejected, match="not_unknown"):
        rules.check_reconcile(replace(action, effect=Effect.UNVERIFIED), later)


def test_completion_gaps() -> None:
    done = _action(status=ActionStatus.DONE, effect=Effect.UNVERIFIED, finished_at=NOW)
    after = replace(OBS, captured_at=NOW + timedelta(seconds=1))
    assert rules.completion_gaps(actions=[done], evidence=[after], device_ids=[DEVICE.id]) == []
    assert rules.completion_gaps(actions=[done], evidence=[], device_ids=[DEVICE.id])
    assert rules.completion_gaps(actions=[done], evidence=[OBS], device_ids=[DEVICE.id])
    assert rules.completion_gaps(
        actions=[replace(done, effect=Effect.UNKNOWN)], evidence=[after], device_ids=[DEVICE.id]
    )
    assert rules.completion_gaps(
        actions=[_action(status=ActionStatus.SENT)], evidence=[after], device_ids=[DEVICE.id]
    )
    assert rules.completion_gaps(actions=[], evidence=[], device_ids=[]) == []


def test_an_earlier_screenshot_may_back_a_change_if_one_after_the_last_input_is_cited() -> None:
    # A task of several settings: the first change is no longer on screen at the end.
    done = _action(status=ActionStatus.DONE, effect=Effect.UNVERIFIED, finished_at=NOW)
    after = replace(OBS, id=uuid4(), captured_at=NOW + timedelta(seconds=1))
    gaps = rules.completion_gaps(actions=[done], evidence=[OBS, after], device_ids=[DEVICE.id])
    assert gaps == []
    gaps = rules.completion_gaps(actions=[done], evidence=[OBS], device_ids=[DEVICE.id])
    assert "taken after your last input" in gaps[0]  # earlier ones alone never do
    other = replace(after, device_id=uuid4())
    twice = replace(done, device_id=other.device_id, id=uuid4())
    assert rules.completion_gaps(  # each phone that was touched needs its own current shot
        actions=[done, twice], evidence=[after], device_ids=[DEVICE.id, other.device_id]
    )


def test_only_an_action_a_person_confirmed_had_no_effect_is_tried_again() -> None:
    person = uuid4()
    confirmed = _action(effect=Effect.NO_EFFECT, reconciled_by=person)
    assert rules.retry_of([confirmed]) == confirmed.id
    assert rules.retry_of([_action(effect=Effect.SUCCESS, reconciled_by=person)]) is None
    assert rules.retry_of([_action(effect=Effect.NO_EFFECT)]) is None  # e.g. an app search
    assert rules.retry_of([confirmed, _action(effect=Effect.UNVERIFIED)]) is None  # not the latest
    assert rules.retry_of([]) is None


def test_citation_marks_a_model_leaves_are_not_shown() -> None:
    marked = "About 13-18 a box.\ue200cite\ue202turn1view4\ue202turn0search1\ue201 Source: 1yaowang"
    assert rules.shown_text(marked) == "About 13-18 a box. Source: 1yaowang"
    assert rules.shown_text("plain text") == "plain text"


def test_the_fast_check_stands_only_when_it_is_sure_nothing_sensitive_happens() -> None:
    clear = {
        e: 0.02
        for e in [
            "purchase",
            "payment",
            "send_message",
            "share_data",
            "account_change",
            "delete_data",
            "change_data",
            "consent",
        ]
    }
    verdict = rules.fast_verdict(clear)
    assert verdict is not None and verdict.sensitive is False and verdict.effects == []
    saving = rules.fast_verdict(clear | {"change_data": 0.9})
    assert saving is not None and saving.effects == ["change_data"]
    assert rules.fast_verdict(clear | {"send_message": 0.1}) is None
    assert rules.fast_verdict(clear | {"consent": 0.5}) is None  # a permission prompt
    assert rules.fast_verdict({k: v for k, v in clear.items() if k != "payment"}) is None


def test_a_goal_message_names_the_new_goal_and_any_other_message_is_just_a_message() -> None:
    goal = rules.GoalCommand
    assert rules.goal_command("/goal  Check the battery ") == goal("set", "Check the battery")
    assert rules.goal_command("/GOAL\nCheck the battery") == goal("set", "Check the battery")
    assert rules.goal_command("Check the battery /goal") is None
    assert rules.goal_command("/goals are hard") is None
    assert rules.goal_command("/goal clear") == goal("clear")
    assert rules.goal_command("/goal Pause") == goal("pause")
    assert rules.goal_command("/goal resume") == goal("resume")
    assert rules.goal_command("/goal clear the cache") == goal("set", "clear the cache")
    with pytest.raises(Rejected):
        rules.goal_command("/goal")


def test_the_agent_sets_a_goal_only_on_a_task_that_never_had_one() -> None:
    rules.check_agent_goal("", ever_set=False)
    with pytest.raises(Rejected) as theirs:
        rules.check_agent_goal("Buy two", ever_set=True)
    assert theirs.value.code == "goal_exists"
    with pytest.raises(Rejected) as cleared:
        rules.check_agent_goal("", ever_set=True)
    assert cleared.value.code == "goal_cleared"


def test_time_counts_only_while_the_agent_works_and_overlaps_once() -> None:
    t0 = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    at = lambda s: t0 + timedelta(seconds=s)  # noqa: E731
    work = [(at(0), at(4)), (at(2), at(6)), (at(60), at(65))]  # a wait in between
    assert rules.active_ms(work, [(at(0), at(100))]) == 11_000
    assert rules.active_ms(work, [(at(3), at(62))]) == 5_000  # only inside the window


def test_a_goal_s_time_stops_while_it_is_paused_and_after_it_is_cleared() -> None:
    t0 = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    at = lambda s: t0 + timedelta(seconds=s)  # noqa: E731
    changes = [(at(10), "set"), (at(20), "pause"), (at(40), "resume"), (at(50), "clear")]
    assert rules.goal_windows(t0, False, changes, at(99)) == [(at(10), at(20)), (at(40), at(50))]
    # Set with the first message: from the start; a rewrite does not stop the clock.
    assert rules.goal_windows(t0, True, [(at(30), "set")], at(60)) == [(t0, at(60))]


def test_an_option_chosen_by_its_text_cannot_be_one_with_an_effect() -> None:
    rules.check_select("select_option", [])
    rules.check_select("tap", ["payment"])  # a tap is checked where it lands, by the screenshot
    with pytest.raises(Rejected, match="select_not_allowed"):
        rules.check_select("select_option", ["payment"])


def test_choosing_an_option_looks_like_a_tap_to_the_policy_and_the_loop_check() -> None:
    assert "select_option" in rules.ASSESSED_KINDS
    same = {"kind": "select_option", "x": 10, "y": 500, "value": "China +86"}
    assert rules._same_input(same, same | {"x": 400})
    assert not rules._same_input(same, same | {"value": "Japan +81"})
    assert rules.text_flags({"kind": "select_option", "x": 1, "y": 2, "value": "\u00a5 500"}) == [
        "money"
    ]


def test_a_sub_agent_s_name_is_one_short_line_or_none() -> None:
    assert rules.agent_name("  iOS\n Agent ") == "iOS Agent"
    assert rules.agent_name("x" * 50) == "x" * rules.AGENT_NAME_MAX
    assert rules.agent_name("   ") is None and rules.agent_name(None) is None


def test_a_run_that_stopped_for_the_user_did_not_ask_a_question() -> None:
    assert rules.asked_question("Which account?")
    assert not any(
        rules.asked_question(d) for d in ("repeating", "unconfirmed", "plan", "model error: 500")
    )


def test_only_the_users_unread_message_to_the_main_agent_is_queued() -> None:
    def queued(**kw: object) -> bool:
        base: dict[str, object] = {
            "kind": "note",
            "run_id": None,
            "seq": 5,
            "read_through": 4,
            "seen": (),
            "live": True,
        }
        return rules.queued_input(**(base | kw))  # type: ignore[arg-type]

    assert queued()
    assert not queued(read_through=5)  # a step read it
    assert not queued(seen={5})  # a side turn read it
    assert not queued(run_id="r2")  # an answer to a sub-agent
    assert not queued(kind="agent")
    assert not queued(kind="system")  # written for the person, not by them
    assert not queued(live=False)  # the task is finished


def turn_end(**kw: object) -> rules.TurnEnd:
    base: dict[str, object] = {
        "completed": False,
        "plan": False,
        "repeating": False,
        "open_calls": False,
        "goal_continue": False,
        "question": None,
        "ask_main": False,
        "called": True,
        "reply": "",
        "searched": False,
        "in_circles": False,
    }
    return rules.turn_end(**(base | kw))  # type: ignore[arg-type]


def test_a_completion_ends_the_run_whatever_else_the_turn_did() -> None:
    end = turn_end(completed=True, repeating=True, open_calls=True, question="Which one?")
    assert end.status == RunStatus.COMPLETED


def test_a_run_stops_for_the_user_for_a_plan_or_a_loop() -> None:
    held = {"open_calls": True, "question": "Which one?"}
    assert turn_end(plan=True, repeating=True, **held) == rules.TurnEnd(
        RunStatus.WAITING, WaitReason.USER_INPUT, "plan"
    )
    assert turn_end(repeating=True, **held) == rules.TurnEnd(
        RunStatus.WAITING, WaitReason.USER_INPUT, "repeating", notice="run.repeating"
    )


def test_open_tool_calls_hold_the_run_for_their_own_reason() -> None:
    end = turn_end(open_calls=True, question="Which one?", goal_continue=True)
    assert end == rules.TurnEnd(RunStatus.WAITING)


def test_three_turns_in_a_row_of_nothing_but_web_searches_are_searching_in_circles() -> None:
    assert rules.searching_in_circles([False, True, True, True])
    assert not rules.searching_in_circles([True, True, False, True])
    assert not rules.searching_in_circles([True, True])


def test_a_turn_that_only_searched_the_web_goes_on_unless_it_searched_in_circles() -> None:
    assert turn_end(called=False, searched=True).status == RunStatus.RUNNABLE
    end = turn_end(called=False, searched=True, in_circles=True)
    assert end.status == RunStatus.WAITING and end.question is not None
    assert end.question.startswith("I searched the web several times")


def test_an_unmet_goal_sends_the_agent_back_to_work_after_a_reply_without_tools() -> None:
    assert turn_end(goal_continue=True, called=False, reply="Done").status == RunStatus.RUNNABLE


def test_a_sub_agent_s_question_to_the_main_agent_waits_for_a_side_turn() -> None:
    end = turn_end(question="Which account?", ask_main=True)
    assert end == rules.TurnEnd(
        RunStatus.WAITING,
        WaitReason.AGENT,
        "Which account?",
        question="Which account?",
        to_main=True,
        asked=True,
    )


def test_a_question_or_a_reply_without_tools_waits_for_the_user_in_the_agent_s_words() -> None:
    asked = turn_end(question="Which account?", reply="Hello")
    assert asked == rules.TurnEnd(
        RunStatus.WAITING,
        WaitReason.USER_INPUT,
        "Which account?",
        question="Which account?",
        asked=True,
    )
    replied = turn_end(called=False, reply="I could not find it.")
    assert replied.detail == replied.question == "I could not find it." and not replied.asked
    silent = turn_end(called=False)
    assert silent.question == "The agent is waiting for you." and not silent.asked
    assert len(turn_end(question="x" * 2000).detail or "") == 1000


def test_a_turn_whose_tool_calls_all_resolved_goes_on() -> None:
    assert turn_end() == rules.TurnEnd(RunStatus.RUNNABLE)


def _at(minute: int) -> datetime:
    return datetime(2026, 10, 22, 8, 0, tzinfo=UTC) + timedelta(minutes=minute)


def test_a_task_s_latest_part_goes_back_whole_rounds_while_they_fit() -> None:
    rounds = [_at(0), _at(10), _at(20), _at(30)]
    # 100 actions in the round at 30; with the 150 of the round at 20 it would be 250.
    actions = [_at(20)] * 150 + [_at(30)] * 100

    assert rules.part_start(rounds, actions) == _at(30)
    # The part before it: the three earlier rounds hold 150, so it reaches the start.
    assert rules.part_start(rounds, actions, before=_at(30)) is None


def test_a_part_holds_at_least_one_round_however_many_actions_it_has() -> None:
    rounds = [_at(0), _at(10)]
    assert rules.part_start(rounds, [_at(10)] * 900) == _at(10)


def test_a_part_of_many_small_rounds_stops_at_twenty_rounds() -> None:
    rounds = [_at(n) for n in range(30)]
    assert rules.part_start(rounds, [_at(n) for n in range(30)]) == _at(10)


def test_a_short_task_is_one_part() -> None:
    assert rules.part_start([_at(0), _at(5)], [_at(1), _at(6)]) is None
