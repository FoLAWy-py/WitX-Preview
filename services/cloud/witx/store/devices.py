"""Gateways, devices and device leases."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import Device, Lease
from witx.store.db import execute, fetch, fetch_one

_DEVICE = """id, tenant_id, gateway_id, native_id, platform, name, capabilities, online, epoch,
             sensitive_apps, problems, enabled_at"""


def _device(row: RowMapping) -> Device:
    return Device(
        id=row["id"],
        tenant_id=row["tenant_id"],
        gateway_id=row["gateway_id"],
        native_id=row["native_id"],
        platform=row["platform"],
        name=row["name"],
        capabilities=list(row["capabilities"]),
        online=row["online"],
        epoch=row["epoch"],
        sensitive_apps=list(row["sensitive_apps"]),
        problems=list(row["problems"]),
        enabled=row["enabled_at"] is not None,
    )


def _lease(row: RowMapping) -> Lease:
    return Lease(
        device_id=row["device_id"],
        tenant_id=row["tenant_id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        epoch=row["epoch"],
        stop_id=row["stop_id"],
        takeover_id=row["takeover_id"],
        takeover_user=row["takeover_user"],
        takeover_expires_at=row["takeover_expires_at"],
    )


async def gateway_by_token(conn: AsyncConnection, token_hash: bytes) -> tuple[UUID, UUID] | None:
    row = await fetch_one(conn, "SELECT * FROM witx_gateway_by_token(:h)", h=token_hash)
    return (row["gateway_id"], row["tenant_id"]) if row else None


async def insert_gateway(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    name: str,
    token_hash: bytes | None = None,
    enroll_code_hash: bytes | None = None,
    enroll_expires_at: datetime | None = None,
) -> UUID:
    """A Gateway, either waiting to enroll with a one-time code or already holding a
    credential (tests and tools that hand one over directly)."""
    gateway_id = uuid4()
    await execute(
        conn,
        """INSERT INTO gateways (id, tenant_id, name, token_hash, enroll_code_hash,
                                 enroll_expires_at, enrolled_at)
           VALUES (:id, :t, :n, :h, :c, :e,
                   CASE WHEN CAST(:h AS bytea) IS NULL THEN NULL ELSE now() END)""",
        id=gateway_id,
        t=tenant_id,
        n=name,
        h=token_hash,
        c=enroll_code_hash,
        e=enroll_expires_at,
    )
    return gateway_id


async def enroll_gateway(
    conn: AsyncConnection, *, code_hash: bytes, token_hash: bytes, host: str, now: datetime
) -> UUID | None:
    """Spends a one-time enrollment code (under the tenant's row-level security)."""
    row = await fetch_one(
        conn,
        """UPDATE gateways
              SET token_hash = :t, enroll_code_hash = NULL, enroll_expires_at = NULL,
                  host = :host, enrolled_at = :now
            WHERE enroll_code_hash = :c AND enroll_expires_at > :now AND revoked_at IS NULL
            RETURNING id""",
        c=code_hash,
        t=token_hash,
        host=host,
        now=now,
    )
    return row["id"] if row else None


async def issue_next_credential(conn: AsyncConnection, gateway_id: UUID, token_hash: bytes) -> None:
    """A rotation: the new credential replaces the current one when first used."""
    await execute(
        conn,
        "UPDATE gateways SET next_token_hash = :h WHERE id = :g",
        g=gateway_id,
        h=token_hash,
    )


async def set_gateway_disabled(conn: AsyncConnection, gateway_id: UUID, disabled: bool) -> bool:
    """Disables a Gateway or enables it again; its credential is kept."""
    value = "coalesce(disabled_at, now())" if disabled else "NULL"
    count = await execute(
        conn,
        f"UPDATE gateways SET disabled_at = {value} WHERE id = :g AND revoked_at IS NULL",
        g=gateway_id,
    )
    return count == 1


async def revoke_gateway(conn: AsyncConnection, gateway_id: UUID) -> bool:
    """Revokes a Gateway for good: every credential and code it had stops working."""
    count = await execute(
        conn,
        """UPDATE gateways
              SET revoked_at = now(), token_hash = NULL, next_token_hash = NULL,
                  enroll_code_hash = NULL, enroll_expires_at = NULL
            WHERE id = :g AND revoked_at IS NULL""",
        g=gateway_id,
    )
    return count == 1


async def gateway_known(conn: AsyncConnection, gateway_id: UUID) -> bool:
    """Whether a Gateway exists and is not revoked (revoking is for good)."""
    row = await fetch_one(
        conn, "SELECT 1 FROM gateways WHERE id = :g AND revoked_at IS NULL", g=gateway_id
    )
    return row is not None


async def gateway_allowed(conn: AsyncConnection, gateway_id: UUID) -> bool:
    """Whether a connected Gateway may stay connected (not revoked or disabled)."""
    row = await fetch_one(
        conn,
        "SELECT 1 FROM gateways WHERE id = :g AND revoked_at IS NULL AND disabled_at IS NULL",
        g=gateway_id,
    )
    return row is not None


async def credential_issued_at(conn: AsyncConnection, gateway_id: UUID) -> datetime | None:
    row = await fetch_one(
        conn,
        "SELECT coalesce(rotated_at, enrolled_at) AS issued FROM gateways WHERE id = :g",
        g=gateway_id,
    )
    return row["issued"] if row else None


async def device_counts(conn: AsyncConnection) -> dict[UUID, int]:
    """How many devices each Gateway has reported."""
    rows = await fetch(conn, "SELECT gateway_id, count(*) AS n FROM devices GROUP BY gateway_id")
    return {r["gateway_id"]: int(r["n"]) for r in rows}


async def list_gateways(conn: AsyncConnection) -> list[dict[str, object]]:
    rows = await fetch(
        conn,
        """SELECT g.id, t.name AS tenant, g.name, g.host, g.enrolled_at, g.rotated_at,
                  g.disabled_at, g.revoked_at, g.last_seen_at, g.enroll_expires_at
             FROM gateways g JOIN tenants t ON t.id = g.tenant_id ORDER BY t.name, g.name""",
    )
    return [dict(r) for r in rows]


async def sync_gateway_devices(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    gateway_id: UUID,
    devices: list[dict[str, object]],
) -> list[Device]:
    """Registers what a Gateway reports, on connecting and whenever a device changes.
    Devices it no longer lists go offline. ``problems`` say why a device cannot take tasks
    (device pool, #33). A new device waits for an owner to enable it; once enabled, it
    keeps the name the owner gave it."""
    await execute(conn, "UPDATE gateways SET last_seen_at = now() WHERE id = :g", g=gateway_id)
    await execute(conn, "UPDATE devices SET online = false WHERE gateway_id = :g", g=gateway_id)
    result = []
    for d in devices:
        row = await fetch_one(
            conn,
            f"""INSERT INTO devices (id, tenant_id, gateway_id, native_id, platform, name,
                                     capabilities, sensitive_apps, problems, online,
                                     last_seen_at)
                VALUES (:id, :t, :g, :native_id, :platform, :name, :caps, :sensitive,
                        :problems, true, now())
                ON CONFLICT (gateway_id, native_id) DO UPDATE
                   SET platform = EXCLUDED.platform,
                       name = CASE WHEN devices.enabled_at IS NULL THEN EXCLUDED.name
                                   ELSE devices.name END,
                       capabilities = EXCLUDED.capabilities,
                       sensitive_apps = EXCLUDED.sensitive_apps,
                       problems = EXCLUDED.problems,
                       online = true, last_seen_at = now()
                RETURNING {_DEVICE}""",
            id=uuid4(),
            t=tenant_id,
            g=gateway_id,
            native_id=d["native_id"],
            platform=d["platform"],
            name=d["name"],
            caps=d["capabilities"],
            sensitive=d.get("sensitive_apps", []),
            problems=d.get("problems", []),
        )
        assert row is not None
        result.append(_device(row))
    return result


async def set_gateway_offline(conn: AsyncConnection, gateway_id: UUID) -> list[UUID]:
    rows = await fetch(
        conn,
        "UPDATE devices SET online = false WHERE gateway_id = :g RETURNING id",
        g=gateway_id,
    )
    return [r["id"] for r in rows]


async def list_all(conn: AsyncConnection) -> list[Device]:
    """The tenant's devices, except those of revoked Gateways (they cannot come back)."""
    rows = await fetch(
        conn,
        f"""SELECT {_DEVICE} FROM devices
             WHERE gateway_id NOT IN (SELECT id FROM gateways WHERE revoked_at IS NOT NULL)
             ORDER BY name""",
    )
    return [_device(r) for r in rows]


async def get(conn: AsyncConnection, device_id: UUID, *, lock: bool = False) -> Device | None:
    row = await fetch_one(
        conn,
        f"SELECT {_DEVICE} FROM devices WHERE id = :id" + (" FOR UPDATE" if lock else ""),
        id=device_id,
    )
    return _device(row) if row else None


async def for_gateway(conn: AsyncConnection, gateway_id: UUID) -> list[Device]:
    rows = await fetch(conn, f"SELECT {_DEVICE} FROM devices WHERE gateway_id = :g", g=gateway_id)
    return [_device(r) for r in rows]


# ---------------------------------------------------------------- leases

_LEASE = """device_id, tenant_id, task_id, run_id, epoch, stop_id, takeover_id, takeover_user,
            takeover_expires_at"""


async def lease(conn: AsyncConnection, device_id: UUID, *, lock: bool = False) -> Lease | None:
    row = await fetch_one(
        conn,
        f"SELECT {_LEASE} FROM device_leases WHERE device_id = :d"
        + (" FOR UPDATE" if lock else ""),
        d=device_id,
    )
    return _lease(row) if row else None


async def leases(conn: AsyncConnection, device_ids: list[UUID]) -> list[Lease]:
    rows = await fetch(
        conn, f"SELECT {_LEASE} FROM device_leases WHERE device_id = ANY(:d)", d=device_ids
    )
    return [_lease(r) for r in rows]


async def task_leases(conn: AsyncConnection, task_id: UUID) -> list[Lease]:
    rows = await fetch(conn, f"SELECT {_LEASE} FROM device_leases WHERE task_id = :t", t=task_id)
    return [_lease(r) for r in rows]


async def bump_epoch(conn: AsyncConnection, device_id: UUID) -> int:
    row = await fetch_one(
        conn, "UPDATE devices SET epoch = epoch + 1 WHERE id = :d RETURNING epoch", d=device_id
    )
    assert row is not None
    return int(row["epoch"])


async def grant_lease(
    conn: AsyncConnection, *, tenant_id: UUID, device_id: UUID, task_id: UUID, run_id: UUID
) -> Lease:
    """Gives a run exclusive input on a device under a new epoch.

    The caller must hold the device row lock and have checked that the device is free
    or already leased to the same task.
    """
    epoch = await bump_epoch(conn, device_id)
    row = await fetch_one(
        conn,
        f"""INSERT INTO device_leases (device_id, tenant_id, task_id, run_id, epoch)
            VALUES (:d, :t, :task, :run, :epoch)
            ON CONFLICT (device_id) DO UPDATE
               SET run_id = EXCLUDED.run_id, epoch = EXCLUDED.epoch, task_id = EXCLUDED.task_id,
                   stop_id = NULL, stop_requested_at = NULL, acquired_at = now(),
                   takeover_id = NULL, takeover_user = NULL, takeover_expires_at = NULL
            RETURNING {_LEASE}""",
        d=device_id,
        t=tenant_id,
        task=task_id,
        run=run_id,
        epoch=epoch,
    )
    assert row is not None
    return _lease(row)


async def epochs(conn: AsyncConnection, device_ids: list[UUID]) -> dict[UUID, int]:
    rows = await fetch(conn, "SELECT id, epoch FROM devices WHERE id = ANY(:d)", d=device_ids)
    return {r["id"]: int(r["epoch"]) for r in rows}


async def hold_for_person(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    device_id: UUID,
    takeover_id: UUID,
    user_id: UUID,
    expires_at: datetime,
) -> Lease:
    """Gives a person, outside any task, exclusive input on a device under a new epoch.

    The caller must hold the device row lock and have checked that no task holds it
    and no one else controls it."""
    epoch = await bump_epoch(conn, device_id)
    row = await fetch_one(
        conn,
        f"""INSERT INTO device_leases (device_id, tenant_id, epoch, takeover_id, takeover_user,
                                       takeover_expires_at)
            VALUES (:d, :t, :epoch, :takeover, :u, :e)
            ON CONFLICT (device_id) DO UPDATE
               SET task_id = NULL, run_id = NULL, epoch = EXCLUDED.epoch, stop_id = NULL,
                   stop_requested_at = NULL, acquired_at = now(),
                   takeover_id = EXCLUDED.takeover_id, takeover_user = EXCLUDED.takeover_user,
                   takeover_expires_at = EXCLUDED.takeover_expires_at
            RETURNING {_LEASE}""",
        d=device_id,
        t=tenant_id,
        epoch=epoch,
        takeover=takeover_id,
        u=user_id,
        e=expires_at,
    )
    assert row is not None
    return _lease(row)


async def request_stop(conn: AsyncConnection, device_id: UUID) -> UUID:
    stop_id = uuid4()
    await execute(
        conn,
        """UPDATE device_leases SET stop_id = :s, stop_requested_at = now()
            WHERE device_id = :d""",
        d=device_id,
        s=stop_id,
    )
    return stop_id


async def set_takeover(
    conn: AsyncConnection,
    device_id: UUID,
    *,
    takeover_id: UUID,
    user_id: UUID,
    expires_at: datetime,
) -> None:
    """Marks the lease's current epoch as a person's. The caller set a new epoch first."""
    await execute(
        conn,
        """UPDATE device_leases SET takeover_id = :t, takeover_user = :u,
                  takeover_expires_at = :e
            WHERE device_id = :d""",
        d=device_id,
        t=takeover_id,
        u=user_id,
        e=expires_at,
    )


async def extend_takeover(conn: AsyncConnection, takeover_id: UUID, expires_at: datetime) -> None:
    await execute(
        conn,
        "UPDATE device_leases SET takeover_expires_at = :e WHERE takeover_id = :t",
        t=takeover_id,
        e=expires_at,
    )


async def lease_by_takeover(
    conn: AsyncConnection, takeover_id: UUID, *, lock: bool = False
) -> Lease | None:
    row = await fetch_one(
        conn,
        f"SELECT {_LEASE} FROM device_leases WHERE takeover_id = :t"
        + (" FOR UPDATE" if lock else ""),
        t=takeover_id,
    )
    return _lease(row) if row else None


async def clear_stop(conn: AsyncConnection, device_id: UUID) -> None:
    """The barrier was confirmed and the device stays with its task (a pause)."""
    await execute(
        conn,
        "UPDATE device_leases SET stop_id = NULL, stop_requested_at = NULL WHERE device_id = :d",
        d=device_id,
    )


async def release_lease(conn: AsyncConnection, device_id: UUID) -> None:
    await execute(conn, "DELETE FROM device_leases WHERE device_id = :d", d=device_id)


async def pool_names(conn: AsyncConnection) -> dict[UUID, str]:
    """Each Gateway's name: the device pool its devices belong to."""
    rows = await fetch(conn, "SELECT id, name FROM gateways")
    return {r["id"]: r["name"] for r in rows}


async def set_enabled(
    conn: AsyncConnection, device_id: UUID, *, user_id: UUID | None, enabled: bool, name: str
) -> None:
    """Enables (with the name the owner gave it) or disables a device, and records who
    (a change replayed after a restore by someone no longer known is not recorded)."""
    await execute(
        conn,
        """UPDATE devices SET name = :name,
                  enabled_at = CASE WHEN :on THEN now() END,
                  enabled_by = CASE WHEN :on THEN CAST(:u AS uuid) END
            WHERE id = :d""",
        d=device_id,
        name=name,
        on=enabled,
        u=user_id,
    )
    await record_change(conn, device_id, user_id, "enabled" if enabled else "disabled", name)


async def record_change(
    conn: AsyncConnection, device_id: UUID, user_id: UUID | None, change: str, name: str
) -> None:
    await execute(
        conn,
        """INSERT INTO device_changes (id, tenant_id, device_id, user_id, change, name)
           SELECT :id, tenant_id, id, CAST(:u AS uuid), :change, :name FROM devices
            WHERE id = :d AND EXISTS (SELECT 1 FROM users WHERE id = CAST(:u AS uuid))""",
        id=uuid4(),
        d=device_id,
        u=user_id,
        change=change,
        name=name,
    )


async def changes(conn: AsyncConnection, device_id: UUID) -> list[dict[str, Any]]:
    """Who enabled, disabled or took over the device, newest first."""
    rows = await fetch(
        conn,
        """SELECT c.change, c.name, u.display_name AS by, c.created_at AS at
             FROM device_changes c JOIN users u ON u.id = c.user_id
            WHERE c.device_id = :d ORDER BY c.created_at DESC""",
        d=device_id,
    )
    return [dict(r) for r in rows]
