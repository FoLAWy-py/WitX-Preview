"""Tenants, users and memberships. These tables are readable without a tenant scope."""

from uuid import UUID, uuid4

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.store.db import execute, fetch, fetch_one


async def user_by_subject(conn: AsyncConnection, subject: str) -> RowMapping | None:
    return await fetch_one(conn, "SELECT id, display_name FROM users WHERE subject = :s", s=subject)


async def memberships(conn: AsyncConnection, user_id: UUID) -> list[RowMapping]:
    return list(
        await fetch(
            conn,
            """SELECT m.tenant_id, t.name AS tenant_name, m.role
                 FROM memberships m JOIN tenants t ON t.id = m.tenant_id
                WHERE m.user_id = :u ORDER BY t.name""",
            u=user_id,
        )
    )


async def allows_full_access(conn: AsyncConnection, tenant_id: UUID) -> bool:
    row = await fetch_one(conn, "SELECT allow_full_access FROM tenants WHERE id = :t", t=tenant_id)
    return bool(row and row["allow_full_access"])


async def fast_risk_check(conn: AsyncConnection, tenant_id: UUID) -> bool:
    row = await fetch_one(conn, "SELECT fast_risk_check FROM tenants WHERE id = :t", t=tenant_id)
    return bool(row and row["fast_risk_check"])


async def allowed_providers(conn: AsyncConnection, tenant_id: UUID) -> list[str] | None:
    """The owner's choice of model providers; None until they choose."""
    row = await fetch_one(conn, "SELECT allowed_providers FROM tenants WHERE id = :t", t=tenant_id)
    value = row["allowed_providers"] if row else None
    return None if value is None else list(value)


async def set_allowed_providers(conn: AsyncConnection, tenant_id: UUID, chosen: list[str]) -> None:
    await execute(
        conn, "UPDATE tenants SET allowed_providers = :p WHERE id = :t", p=chosen, t=tenant_id
    )


async def model_fallback(conn: AsyncConnection, tenant_id: UUID) -> bool:
    row = await fetch_one(conn, "SELECT model_fallback FROM tenants WHERE id = :t", t=tenant_id)
    return bool(row and row["model_fallback"])


async def auto_memory(conn: AsyncConnection, tenant_id: UUID, user_id: UUID) -> bool:
    """Whether the agent keeps what it learns for this person on its own."""
    row = await fetch_one(
        conn,
        "SELECT auto_memory FROM memberships WHERE tenant_id = :t AND user_id = :u",
        t=tenant_id,
        u=user_id,
    )
    return bool(row is None or row["auto_memory"])


async def screenshot_retention_days(
    conn: AsyncConnection, tenant_id: UUID, user_id: UUID
) -> int | None:
    """How many days this person keeps their finished tasks' screenshots; None:
    the deployment's setting."""
    row = await fetch_one(
        conn,
        "SELECT screenshot_retention_days FROM memberships WHERE tenant_id = :t AND user_id = :u",
        t=tenant_id,
        u=user_id,
    )
    return row["screenshot_retention_days"] if row else None


async def set_screenshot_retention_days(
    conn: AsyncConnection, tenant_id: UUID, user_id: UUID, days: int | None
) -> None:
    await execute(
        conn,
        """UPDATE memberships SET screenshot_retention_days = :d
            WHERE tenant_id = :t AND user_id = :u""",
        d=days,
        t=tenant_id,
        u=user_id,
    )


async def set_auto_memory(conn: AsyncConnection, tenant_id: UUID, user_id: UUID, on: bool) -> None:
    await execute(
        conn,
        "UPDATE memberships SET auto_memory = :on WHERE tenant_id = :t AND user_id = :u",
        on=on,
        t=tenant_id,
        u=user_id,
    )


async def set_model_fallback(conn: AsyncConnection, tenant_id: UUID, on: bool) -> None:
    await execute(conn, "UPDATE tenants SET model_fallback = :on WHERE id = :t", on=on, t=tenant_id)


async def set_fast_risk_check(conn: AsyncConnection, tenant_id: UUID, on: bool) -> None:
    await execute(
        conn, "UPDATE tenants SET fast_risk_check = :on WHERE id = :t", on=on, t=tenant_id
    )


async def set_allow_full_access(conn: AsyncConnection, tenant_id: UUID, allowed: bool) -> None:
    await execute(
        conn, "UPDATE tenants SET allow_full_access = :a WHERE id = :t", a=allowed, t=tenant_id
    )


# The functions below are for provisioning (`witx admin ...`), run as the schema owner.


async def ensure_tenant(conn: AsyncConnection, name: str) -> UUID:
    row = await fetch_one(conn, "SELECT id FROM tenants WHERE name = :n", n=name)
    if row:
        return UUID(str(row["id"]))
    tenant_id = uuid4()
    await execute(conn, "INSERT INTO tenants (id, name) VALUES (:id, :n)", id=tenant_id, n=name)
    return tenant_id


async def ensure_user(conn: AsyncConnection, subject: str, display_name: str) -> UUID:
    row = await user_by_subject(conn, subject)
    if row:
        return UUID(str(row["id"]))
    user_id = uuid4()
    await execute(
        conn,
        "INSERT INTO users (id, subject, display_name) VALUES (:id, :s, :n)",
        id=user_id,
        s=subject,
        n=display_name,
    )
    return user_id


async def ensure_membership(
    conn: AsyncConnection, tenant_id: UUID, user_id: UUID, role: str = "owner"
) -> None:
    await execute(
        conn,
        """INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)
           ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
        t=tenant_id,
        u=user_id,
        r=role,
    )


async def tenants(conn: AsyncConnection) -> list[RowMapping]:
    """Every tenant (id, name): for work done tenant by tenant under row-level security."""
    return list(await fetch(conn, "SELECT id, name FROM tenants ORDER BY name"))
