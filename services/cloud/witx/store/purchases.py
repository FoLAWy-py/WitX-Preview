"""Purchase intents : where the agent proposes to buy, and the person's choice."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import PurchaseIntent, PurchaseOption
from witx.store.db import execute, fetch, fetch_one, jsonb

_INTENT = """id, tenant_id, task_id, run_id, goal, options, status, chosen, decided_by,
             decided_at, created_at"""


def _option(raw: Mapping[str, Any]) -> PurchaseOption:
    observation = raw.get("observation_id")
    return PurchaseOption(
        device_id=UUID(str(raw["device_id"])),
        payee=str(raw["payee"]),
        amount=str(raw["amount"]),
        currency=str(raw["currency"]),
        summary=str(raw.get("summary", "")),
        observation_id=UUID(str(observation)) if observation else None,
    )


def _intent(row: RowMapping) -> PurchaseIntent:
    return PurchaseIntent(**{**dict(row), "options": [_option(o) for o in row["options"]]})


async def insert(conn: AsyncConnection, intent: PurchaseIntent) -> None:
    await execute(
        conn,
        """INSERT INTO purchase_intents (id, tenant_id, task_id, run_id, goal, options,
                                         status, created_at)
           VALUES (:id, :tenant_id, :task_id, :run_id, :goal, CAST(:options AS jsonb),
                   :status, :created_at)""",
        id=intent.id,
        tenant_id=intent.tenant_id,
        task_id=intent.task_id,
        run_id=intent.run_id,
        goal=intent.goal,
        options=jsonb(
            [
                {
                    "device_id": str(o.device_id),
                    "payee": o.payee,
                    "amount": o.amount,
                    "currency": o.currency,
                    "summary": o.summary,
                    "observation_id": str(o.observation_id) if o.observation_id else None,
                }
                for o in intent.options
            ]
        ),
        status=intent.status,
        created_at=intent.created_at,
    )


async def get(
    conn: AsyncConnection, intent_id: UUID, *, lock: bool = False
) -> PurchaseIntent | None:
    row = await fetch_one(
        conn,
        f"SELECT {_INTENT} FROM purchase_intents WHERE id = :i" + (" FOR UPDATE" if lock else ""),
        i=intent_id,
    )
    return _intent(row) if row else None


async def for_task(conn: AsyncConnection, task_id: UUID) -> list[PurchaseIntent]:
    """The task's purchase intents, oldest first; the last one is the one that counts."""
    rows = await fetch(
        conn,
        f"SELECT {_INTENT} FROM purchase_intents WHERE task_id = :t ORDER BY created_at, id",
        t=task_id,
    )
    return [_intent(r) for r in rows]


async def decide(
    conn: AsyncConnection, intent_id: UUID, *, chosen: int | None, user_id: UUID, now: datetime
) -> None:
    await execute(
        conn,
        """UPDATE purchase_intents SET status = :s, chosen = :c, decided_by = :u, decided_at = :n
            WHERE id = :i AND status = 'proposed'""",
        i=intent_id,
        s="declined" if chosen is None else "chosen",
        c=chosen,
        u=user_id,
        n=now,
    )


async def submission_pending(conn: AsyncConnection, task_id: UUID) -> bool:
    """Whether an order or payment the task submitted has a pending or unknown result."""
    row = await fetch_one(
        conn,
        """SELECT 1 FROM actions WHERE task_id = :t AND purchase_intent_id IS NOT NULL
              AND (status = 'sent' OR effect = 'unknown') LIMIT 1""",
        t=task_id,
    )
    return row is not None
