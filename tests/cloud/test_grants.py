"""Grants : a person approves a held input "and the same kind". Later
inputs of the task on the same phone and app, with the same effects and recipient, go
ahead without asking, a limited number of times; payments and orders never do."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from witx.domain import rules
from witx.domain.model import (
    Action,
    ActionStatus,
    Effect,
    Grant,
    PermissionMode,
    Policy,
    RiskVerdict,
)
from witx.domain.rules import Rejected
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.store import execution
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, now, provision
from tests.cloud.test_flow import tap

DEVICE = uuid4()
APP = "com.example.gallery"


def policy(*effects: str, **kw: Any) -> Policy:
    return Policy(
        mode=PermissionMode.AUTO,
        decision="ask",
        reasons=[],
        declared_effects=list(effects),
        risk=kw.pop(
            "risk",
            RiskVerdict(
                sensitive=bool(set(effects) - {"change_data"}), effects=list(effects), reason=""
            ),
        ),
        app_sensitive=kw.pop("app_sensitive", False),
        text_flags=kw.pop("text_flags", []),
        found_rank=kw.pop("found_rank", None),
    )


def held_action(p: Policy, intent: dict[str, str] | None = None) -> Action:
    return Action(
        id=uuid4(),
        tenant_id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        device_id=DEVICE,
        tool_call_id=uuid4(),
        epoch=1,
        control_revision=1,
        observation_id=uuid4(),
        kind="tap",
        input={"kind": "tap", "x": 1.0, "y": 2.0},
        target="Delete",
        expected_effect="the photo is deleted",
        effects=p.declared_effects,
        digest="d",
        status=ActionStatus.AWAITING_APPROVAL,
        effect=Effect.NOT_ATTEMPTED,
        reason=None,
        post_observation_id=uuid4(),
        created_at=now(),
        policy=p,
        intent=intent,
    )


def grant(*effects: str, terms: dict[str, str] | None = None, **kw: Any) -> Grant:
    return Grant(
        id=uuid4(),
        tenant_id=uuid4(),
        task_id=uuid4(),
        created_from=uuid4(),
        device_id=DEVICE,
        app=APP,
        effects=list(effects),
        terms=terms or {},
        control_revision=kw.pop("control_revision", 1),
        max_uses=kw.pop("max_uses", rules.GRANT_MAX_USES),
        uses=kw.pop("uses", 0),
        created_by=uuid4(),
        created_at=now(),
        expires_at=kw.pop("expires_at", now() + rules.GRANT_TTL),
        **kw,
    )


def covers(g: Grant, p: Policy, terms: dict[str, str] | None = None, **kw: Any) -> bool:
    return rules.grant_covers(
        g,
        policy=p,
        device_id=kw.pop("device_id", DEVICE),
        app=kw.pop("app", APP),
        terms=terms,
        retry=kw.pop("retry", False),
        control_revision=kw.pop("control_revision", 1),
        now=kw.pop("at", now()),
    )


def test_payments_and_orders_are_never_approved_as_a_kind() -> None:
    for effect in ("payment", "purchase"):
        with pytest.raises(Rejected, match="grant_not_allowed"):
            rules.grant_scope(held_action(policy(effect)))
    unassessed = RiskVerdict(sensitive=True, effects=[], reason="timeout", failed=True)
    with pytest.raises(Rejected, match="grant_not_allowed"):
        rules.grant_scope(held_action(policy("delete_data", risk=unassessed)))


def test_inputs_held_for_reasons_other_than_their_effects_are_approved_one_by_one() -> None:
    for held in (
        policy("delete_data", app_sensitive=True),
        policy("send_message", text_flags=["money"]),
        policy(found_rank=1),
        policy(risk=RiskVerdict(sensitive=True, effects=[], reason="unclear")),
    ):
        with pytest.raises(Rejected, match="grant_not_allowed"):
            rules.grant_scope(held_action(held))


def test_a_grant_keeps_a_messages_recipient_and_sender_but_not_its_text() -> None:
    terms = {"sender": "work@example.com", "recipient": "Alice", "body": "Hi"}
    effects, kept = rules.grant_scope(held_action(policy("send_message"), terms))
    assert effects == ["send_message"]
    assert kept == {"sender": "work@example.com", "recipient": "Alice"}


def test_a_grant_covers_the_same_effect_on_the_same_phone_and_app() -> None:
    g = grant("delete_data")
    assert covers(g, policy("delete_data"))
    assert not covers(g, policy("delete_data"), device_id=uuid4())
    assert not covers(g, policy("delete_data"), app="com.example.bank")


def test_a_grant_covers_no_effect_beyond_its_own() -> None:
    assert not covers(grant("delete_data"), policy("delete_data", "share_data"))
    assert not covers(grant("change_data"), policy("send_message"))
    assert not covers(grant("send_message"), policy("change_data"))
    assert covers(grant("change_data"), policy("change_data"))


def test_a_message_grant_covers_only_the_same_recipient_and_sender() -> None:
    g = grant("send_message", terms={"recipient": "Alice"})
    assert covers(g, policy("send_message"), {"recipient": "alice", "body": "Another text"})
    assert not covers(g, policy("send_message"), {"recipient": "Alice Wong", "body": "Hi"})
    assert not covers(
        g, policy("send_message"), {"recipient": "Alice", "sender": "home", "body": "Hi"}
    )


def test_a_grant_never_covers_a_payment_or_a_retry() -> None:
    assert not covers(grant("delete_data"), policy("delete_data", "payment"))
    assert not covers(grant("delete_data"), policy("delete_data"), retry=True)


def test_a_grant_ends_when_revoked_used_up_expired_or_the_task_is_paused_or_resumed() -> None:
    def status(g: Grant, revision: int = 1, at: Any = None) -> str:
        return rules.grant_status(g, control_revision=revision, now=at or now())

    assert status(grant("delete_data")) == "active"
    assert status(grant("delete_data", revoked_at=now(), revoked_by=uuid4())) == "revoked"
    assert status(grant("delete_data", uses=3, max_uses=3)) == "used_up"
    assert status(grant("delete_data"), at=now() + rules.GRANT_TTL) == "expired"
    assert status(grant("delete_data"), revision=2) == "ended"
    assert not covers(grant("delete_data"), policy("delete_data"), control_revision=2)


# ---------------------------------------------------------------- through the system


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def two_deletions(rig: Rig) -> tuple[UUID, UUID]:
    """A task that deletes twice; the first deletion waits for approval. Returns the
    task and that first input."""
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, effects=["delete_data"]),
        lambda r: tap(r, device, effects=["delete_data"]),
    ]
    task = await rig.create_task("Delete the two blurry photos")
    await rig.settle()
    (first,) = await rig.actions(task.id)
    assert first.status == ActionStatus.AWAITING_APPROVAL
    return task.id, first.id


async def decide(rig: Rig, action_id: UUID, **kw: Any) -> None:
    async with rig.db.tenant(rig.tenant.id) as conn:
        await exec_service.decide(
            conn,
            action_id=action_id,
            decision="approve",
            user_id=rig.tenant.user_id,
            now=now(),
            **kw,
        )


async def until_second_action(rig: Rig, task_id: UUID) -> Action:
    """Steps until the second input is admitted, before the Gateway is asked for it."""
    for _ in range(20):
        await rig.worker.run_once()
        actions = await rig.actions(task_id)
        if len(actions) == 2:
            return actions[1]
        await rig.gateway_round()
    raise AssertionError("no second input")


async def test_after_approving_the_same_kind_the_next_such_input_goes_ahead(rig: Rig) -> None:
    task_id, first = await two_deletions(rig)
    await decide(rig, first, similar=True)
    await rig.settle()
    _, second = await rig.actions(task_id)
    assert [a.action_id for a in rig.gateway.actions] == [first, second.id]
    async with rig.db.tenant(rig.tenant.id) as conn:
        approval = await execution.get_approval(conn, second.id)
        (g,) = await execution.task_grants(conn, task_id)
    assert approval is not None and approval.grant_id == g.id and approval.used_at is not None
    assert g.created_from == first and g.uses == 1 and g.effects == ["delete_data"]


async def test_a_grant_is_given_on_the_web_app_only(rig: Rig) -> None:
    _, first = await two_deletions(rig)
    with pytest.raises(Rejected, match="web_approval_required"):
        await decide(rig, first, similar=True, via="mcp")


async def test_revoking_a_grant_puts_its_unsent_inputs_back_before_the_person(rig: Rig) -> None:
    task_id, first = await two_deletions(rig)
    await decide(rig, first, similar=True)
    second = await until_second_action(rig, task_id)
    assert second.status == ActionStatus.QUEUED  # approved by the grant, not yet sent
    async with rig.db.tenant(rig.tenant.id) as conn:
        (g,) = await execution.task_grants(conn, task_id)
        revoked = await exec_service.revoke_grant(conn, grant_id=g.id, user_id=rig.tenant.user_id)
    assert revoked.revoked_at is not None
    await rig.settle()
    _, waiting = await rig.actions(task_id)
    assert waiting.status == ActionStatus.AWAITING_APPROVAL
    assert [a.action_id for a in rig.gateway.actions] == [first]
    async with rig.db.tenant(rig.tenant.id) as conn:
        approval = await execution.get_approval(conn, second.id)
    assert approval is not None and approval.decision is None and approval.grant_id is None


async def test_a_new_goal_ends_the_tasks_grants(rig: Rig) -> None:
    task_id, first = await two_deletions(rig)
    await decide(rig, first, similar=True)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task_id, user_id=rig.tenant.user_id, text="/goal Keep every photo"
        )
        (g,) = await execution.task_grants(conn, task_id)
    assert g.revoked_at is not None
