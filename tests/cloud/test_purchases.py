"""Purchase intents : in a task with several phones, the agent
proposes where to buy and a person chooses; only the chosen phone may order or pay, on
the chosen terms. Tasks with one phone are unaffected."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from witx.domain import rules
from witx.domain.model import PurchaseIntent, PurchaseOption
from witx.domain.rules import Rejected
from witx.services import purchases as purchase_service
from witx.store import purchases
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, now, provision
from tests.cloud.test_flow import tap

PHONE_A, PHONE_B = uuid4(), uuid4()
SHOP_A = PurchaseOption(PHONE_A, "Pet World", "39.90", "CNY", "1 bag, 2 kg")
SHOP_B = PurchaseOption(PHONE_B, "Corner Shop", "42.50", "CNY", "1 bag, 2 kg")
ORDER = {"payee": "Corner Shop", "amount": "¥42.50", "currency": "CNY"}


def intent(status: str = "chosen", chosen: int | None = 1) -> PurchaseIntent:
    return PurchaseIntent(
        id=uuid4(),
        tenant_id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        goal="Cat food",
        options=[SHOP_A, SHOP_B],
        status=status,
        created_at=now(),
        chosen=chosen,
    )


def check(effects: list[str], current: PurchaseIntent | None, **kw: Any) -> UUID | None:
    return rules.check_purchase(
        effects=effects,
        phones=kw.pop("phones", 2),
        intent=current,
        device_id=kw.pop("device_id", PHONE_B),
        terms=kw.pop("terms", ORDER),
    )


def test_with_one_phone_an_order_needs_no_purchase_choice() -> None:
    assert check(["purchase"], None, phones=1) is None


def test_an_input_that_neither_orders_nor_pays_needs_no_choice() -> None:
    assert check(["change_data"], None) is None


def test_with_several_phones_an_order_or_payment_waits_for_the_users_choice() -> None:
    for effect in ("purchase", "payment"):
        with pytest.raises(Rejected, match="purchase_choice_required"):
            check([effect], None)
    with pytest.raises(Rejected, match="purchase_choice_required"):
        check(["purchase"], intent("proposed", None))
    with pytest.raises(Rejected, match="purchase_choice_required"):
        check(["purchase"], intent("declined", None))


def test_only_the_chosen_phone_may_order_on_the_chosen_terms() -> None:
    chosen = intent()
    assert check(["purchase"], chosen) == chosen.id
    with pytest.raises(Rejected, match="purchase_elsewhere"):
        check(["purchase"], chosen, device_id=PHONE_A)


def test_a_changed_price_or_shop_is_proposed_again() -> None:
    with pytest.raises(Rejected, match="purchase_changed"):
        check(["payment"], intent(), terms={**ORDER, "amount": "45.00"})
    with pytest.raises(Rejected, match="purchase_changed"):
        check(["payment"], intent(), terms={**ORDER, "payee": "Pet World"})


def test_options_are_on_the_tasks_phones_and_state_their_terms() -> None:
    rules.check_purchase_options([SHOP_A, SHOP_B], [PHONE_A, PHONE_B])
    with pytest.raises(Rejected, match="invalid_arguments"):
        rules.check_purchase_options([SHOP_A], [PHONE_B])
    with pytest.raises(Rejected, match="invalid_arguments"):
        rules.check_purchase_options(
            [PurchaseOption(PHONE_A, "Pet World", "", "CNY", "")], [PHONE_A]
        )
    with pytest.raises(Rejected, match="invalid_arguments"):
        rules.check_purchase_options([], [PHONE_A])


def test_nothing_new_is_proposed_while_an_order_has_an_unsettled_result() -> None:
    rules.check_new_purchase(pending=False)
    with pytest.raises(Rejected, match="purchase_pending"):
        rules.check_new_purchase(pending=True)


def test_only_the_newest_proposal_is_chosen_and_only_once() -> None:
    proposed = intent("proposed", None)
    rules.check_purchase_choice(proposed, newest=proposed.id, option=1)
    rules.check_purchase_choice(proposed, newest=proposed.id, option=None)  # none of them
    with pytest.raises(Rejected, match="superseded"):
        rules.check_purchase_choice(proposed, newest=uuid4(), option=1)
    with pytest.raises(Rejected, match="already_decided"):
        rules.check_purchase_choice(intent(), newest=proposed.id, option=0)
    with pytest.raises(Rejected, match="invalid_option"):
        rules.check_purchase_choice(proposed, newest=proposed.id, option=2)


# ---------------------------------------------------------------- through the system


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=2)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0), device_info(1)])
    return rig


async def proposed(rig: Rig) -> tuple[UUID, PurchaseIntent]:
    """A two-phone task that tries to order on the second phone, then proposes both."""
    a, b = rig.tenant.device_ids
    bag = {"summary": "1 bag, 2 kg", "observation_id": ""}
    options = [
        {"device_id": str(a), "payee": "Pet World", "amount": "39.90", "currency": "CNY", **bag},
        {"device_id": str(b), "payee": "Corner Shop", "amount": "42.50", "currency": "CNY", **bag},
    ]
    rig.model.script = [
        [call("observe", device_id=b)],
        lambda r: tap(r, b, effects=["purchase"]),
        [call("propose_purchase", goal="Cat food", question="Where to buy?", options=options)],
        lambda r: tap(r, b, effects=["purchase"]),
    ]
    task = await rig.create_task("Buy cat food where it is cheaper")
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        (current,) = await purchases.for_task(conn, task.id)
    return task.id, current


async def choose(rig: Rig, intent_id: UUID, option: int | None) -> None:
    async with rig.db.tenant(rig.tenant.id) as conn:
        await purchase_service.choose(
            conn, intent_id=intent_id, option=option, user_id=rig.tenant.user_id, now=now()
        )


async def test_an_order_waits_for_the_choice_and_then_goes_ahead_on_the_chosen_phone(
    rig: Rig,
) -> None:
    task_id, current = await proposed(rig)
    outputs = await rig.outputs(task_id)
    assert outputs[1]["code"] == "purchase_choice_required"
    assert outputs[2]["status"] == "proposed"
    assert await rig.actions(task_id) == []  # nothing ordered while the person decides
    await choose(rig, current.id, 1)
    await rig.settle()
    (order,) = await rig.actions(task_id)
    assert order.purchase_intent_id == current.id
    assert order.device_id == rig.tenant.device_ids[1]


async def test_an_order_on_a_phone_the_person_did_not_choose_is_refused(rig: Rig) -> None:
    task_id, current = await proposed(rig)
    await choose(rig, current.id, 0)
    await rig.settle()
    assert await rig.actions(task_id) == []
    assert (await rig.outputs(task_id))[-1]["code"] == "purchase_elsewhere"


async def test_choosing_none_tells_the_agent_not_to_buy(rig: Rig) -> None:
    task_id, current = await proposed(rig)
    await choose(rig, current.id, None)
    await rig.settle()
    assert await rig.actions(task_id) == []
    assert (await rig.outputs(task_id))[-1]["code"] == "purchase_choice_required"
