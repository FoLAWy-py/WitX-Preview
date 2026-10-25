"""A message's or payment's terms: stated by the agent, checked against the
screenshot by the independent risk check, and bound to the approval."""

from pathlib import Path
from uuid import uuid4

import pytest
from witx.domain import rules
from witx.domain.model import ActionStatus, PermissionMode, RiskVerdict
from witx.domain.rules import Rejected
from witx.harness.risk import parse_verdict
from witx.store.db import Database

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision

NOTE = {"recipient": "15555215554", "body": "WitX 审批验收 M1"}


def check(effects: list[str], declared: dict | None, seen: dict | None):  # type: ignore[no-untyped-def, type-arg]
    return rules.check_intent(effects=effects, declared=declared, seen=seen)


def test_a_message_or_payment_must_state_its_terms() -> None:
    with pytest.raises(Rejected, match="intent_required") as error:
        check(["send_message"], {"recipient": "Alice"}, None)
    assert "body" in error.value.detail
    with pytest.raises(Rejected, match="intent_required"):
        check(["payment"], None, None)
    assert check(["change_data"], None, None) == (None, [])  # nothing to state


def test_terms_the_screen_contradicts_are_refused() -> None:
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(["send_message"], NOTE, {**NOTE, "body": "WitX 审批验收 M2"})
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(["send_message"], NOTE, {**NOTE, "recipient": "(555) 521-9999"})
    pay = {"payee": "Corner Shop", "amount": "42.50", "currency": "CNY"}
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(["payment"], pay, {**pay, "amount": "¥45.20"})


def test_terms_match_however_the_screen_formats_them() -> None:
    seen = {"recipient": "(555) 521-5554", "body": "WitX  审批验收 M1"}
    assert check(["send_message"], NOTE, seen) == (NOTE, ["terms match the screen"])
    pay = {"payee": "Corner Shop", "amount": "42.5", "currency": "CNY"}
    shown = {"payee": "corner shop", "amount": "¥42.50", "currency": "¥"}
    assert check(["purchase"], pay, shown)[1] == ["terms match the screen"]
    long = "Please bring the meeting materials and the projector adapter tomorrow"
    preview = {"recipient": "Alice", "body": long[:30] + "…"}
    assert check(["send_message"], {"recipient": "Alice", "body": long}, preview)[1] == [
        "terms match the screen"
    ]


def test_a_name_address_or_number_that_merely_contains_the_other_is_not_the_same() -> None:
    message = {"recipient": "张三", "body": "Hi"}
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(["send_message"], message, {**message, "recipient": "张三丰"})
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(
            ["send_message"],
            {**message, "recipient": "2345678"},
            {**message, "recipient": "13812345678"},
        )
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(["purchase"], {**ORDER, "address": "3号楼"}, {**ORDER, "address": "朝阳区13号楼"})


def test_a_whole_word_more_a_country_code_or_text_the_screen_cuts_short_still_matches() -> None:
    message = {"recipient": "13911112959", "body": "Hi"}
    assert check(["send_message"], message, {**message, "recipient": "+86 139 1111 2959"})[1] == [
        "terms match the screen"
    ]
    declared = {**ORDER, "address": "绿地新都会 3 号楼 1201"}
    assert check(["purchase"], declared, {**ORDER, "address": "…3 号楼 1201"})[1] == [
        "terms match the screen"
    ]


def test_terms_the_screen_does_not_show_are_noted_not_trusted() -> None:
    assert check(["send_message"], NOTE, {"recipient": "15555215554"})[1] == [
        "not readable on the screen: body"
    ]
    assert check(["send_message"], NOTE, None)[1] == ["terms not checked against the screen"]


ORDER = {"payee": "悦兔佳大药房", "amount": "20.4", "currency": "CNY"}
CHECKOUT = {**ORDER, "address": "绿地新都会 3 号楼 1201 张三 138****0000"}


def test_a_checkout_s_delivery_address_and_payment_method_are_bound_with_the_amount() -> None:
    declared = {**ORDER, "address": "绿地新都会 3 号楼 1201", "payment_method": "美团支付"}
    shown = {**CHECKOUT, "payment_method": "美团支付 推荐"}
    terms, notes = check(["purchase"], declared, shown)
    assert terms == declared and notes == ["terms match the screen"]


def test_a_delivery_address_the_screen_shows_must_be_stated() -> None:
    with pytest.raises(Rejected, match="intent_required") as error:
        check(["purchase"], ORDER, CHECKOUT)
    assert "address" in error.value.detail


def test_a_delivery_address_the_screen_contradicts_is_refused() -> None:
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(["purchase"], {**ORDER, "address": "公司前台"}, CHECKOUT)


def test_a_payment_without_an_address_or_method_on_screen_binds_only_what_is_there() -> None:
    assert check(["payment"], ORDER, ORDER) == (ORDER, ["terms match the screen"])


def test_the_account_a_message_is_sent_from_is_bound_when_the_screen_shows_it() -> None:
    declared = {**NOTE, "sender": "work@example.com"}
    shown = {**NOTE, "sender": "Work <work@example.com>"}
    assert check(["send_message"], declared, shown) == (declared, ["terms match the screen"])


def test_a_sender_the_screen_shows_must_be_stated() -> None:
    with pytest.raises(Rejected, match="intent_required") as error:
        check(["send_message"], NOTE, {**NOTE, "sender": "work@example.com"})
    assert "sender" in error.value.detail


def test_a_sender_the_screen_contradicts_is_refused() -> None:
    declared = {**NOTE, "sender": "home@example.com"}
    with pytest.raises(Rejected, match="intent_mismatch"):
        check(["send_message"], declared, {**NOTE, "sender": "work@example.com"})


def test_a_message_without_a_sender_on_screen_binds_only_what_is_there() -> None:
    assert check(["send_message"], NOTE, NOTE) == (NOTE, ["terms match the screen"])


def test_an_approval_covers_the_terms_it_was_given() -> None:
    action, device = uuid4(), uuid4()
    tap_input = {"kind": "tap", "x": 1.0, "y": 2.0}
    approved = rules.action_digest(action, device, 1, tap_input, NOTE)
    edited = rules.action_digest(action, device, 1, tap_input, {**NOTE, "body": "other text"})
    assert approved != edited
    assert rules.action_digest(action, device, 1, tap_input) != approved
    other_sender = rules.action_digest(action, device, 1, tap_input, {**NOTE, "sender": "work"})
    assert other_sender != approved


def test_the_risk_check_reports_the_terms_it_sees() -> None:
    output = [
        {
            "type": "function_call",
            "name": "report_risk",
            "arguments": '{"sensitive": true, "effects": ["send_message"], "reason": "sends",'
            ' "seen": {"recipient": "(555) 521-5554", "body": "hi", "payee": null,'
            ' "amount": null, "currency": null}}',
        }
    ]
    assert parse_verdict(output).seen == {"recipient": "(555) 521-5554", "body": "hi"}


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def proposed(rig: Rig, intent: dict | None, seen: dict | None) -> list:  # type: ignore[type-arg]
    device = rig.tenant.device_ids[0]
    rig.risk.verdict = RiskVerdict(
        sensitive=True, effects=["send_message"], reason="sends it", seen=seen
    )
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: [
            call(
                "tap",
                device_id=device,
                observation_id=last_screenshot(r),
                x=540,
                y=1200,
                target="Send button",
                expected_effect="the message is sent",
                effects=["send_message"],
                intent=intent,
            )
        ],
    ]
    task = await rig.create_task("Send the note", permission_mode=PermissionMode.ASK)
    await rig.settle()
    return [await rig.actions(task.id), await rig.outputs(task.id)]


async def test_a_message_whose_terms_match_the_screen_waits_with_them(rig: Rig) -> None:
    actions, _ = await proposed(rig, NOTE, NOTE)
    (send,) = actions
    assert send.status == ActionStatus.AWAITING_APPROVAL and send.intent == NOTE
    assert "terms match the screen" in send.policy.reasons


async def test_a_message_the_screen_contradicts_is_refused_and_never_stored(rig: Rig) -> None:
    actions, outputs = await proposed(rig, NOTE, {**NOTE, "body": "something else"})
    assert actions == []
    assert outputs[1]["code"] == "intent_mismatch"
