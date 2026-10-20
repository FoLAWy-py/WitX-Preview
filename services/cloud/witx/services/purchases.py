"""Purchase intents : in a task with several phones, the agent
proposes where to buy and a person chooses. Runs inside the caller's transaction."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.domain.model import PurchaseIntent, PurchaseOption, Run, Task
from witx.domain.rules import Rejected
from witx.services import lifecycle
from witx.store import devices, purchases, tasks


async def propose(
    conn: AsyncConnection,
    task: Task,
    run: Run,
    *,
    goal: str,
    options: list[PurchaseOption],
    now: datetime,
) -> PurchaseIntent:
    """Records the agent's options; nothing may be ordered or paid until a person chooses."""
    if not goal.strip():
        raise Rejected("invalid_arguments", "say what is being bought")
    rules.check_purchase_options(options, task.device_ids)
    rules.check_new_purchase(pending=await purchases.submission_pending(conn, task.id))
    intent = PurchaseIntent(
        id=uuid4(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        goal=goal.strip()[:2000],
        options=options,
        status="proposed",
        created_at=now,
    )
    await purchases.insert(conn, intent)
    await tasks.emit(conn, task, "purchase.proposed", purchase_id=intent.id)
    return intent


async def choose(
    conn: AsyncConnection, *, intent_id: UUID, option: int | None, user_id: UUID, now: datetime
) -> PurchaseIntent:
    """A person chooses one option of the newest proposal, or none. The agent reads the
    choice as the person's next message."""
    intent = await purchases.get(conn, intent_id, lock=True)
    if intent is None:
        raise Rejected("not_found")
    newest = (await purchases.for_task(conn, intent.task_id))[-1]
    rules.check_purchase_choice(intent, newest=newest.id, option=option)
    await purchases.decide(conn, intent.id, chosen=option, user_id=user_id, now=now)
    await tasks.emit(conn, intent.task_id, "purchase.decided", purchase_id=intent.id, chosen=option)
    if option is None:
        text = f"I chose none of the purchase options for {intent.goal!r}: do not buy it."
    else:
        chosen = intent.options[option]
        device = await devices.get(conn, chosen.device_id)
        text = (
            f"For {intent.goal!r} I chose option {option + 1}: {chosen.payee}, "
            f"{chosen.amount} {chosen.currency}, on {device.name if device else 'that phone'}"
            f" (device {chosen.device_id}). Order and pay only there, on these terms."
        )
    await lifecycle.add_input(
        conn,
        task_id=intent.task_id,
        user_id=user_id,
        text=text,
        run_id=intent.run_id,
        system=True,
    )
    updated = await purchases.get(conn, intent.id)
    assert updated is not None
    return updated
