"""Readiness : can this API process do its work right now? Liveness is
/health; this checks the dependencies a request needs."""

import logging
from uuid import uuid4

from witx.objects import ObjectStore
from witx.store.db import Database, fetch_one

log = logging.getLogger(__name__)

PROBE = b"witx readiness probe"


async def readiness(db: Database, objects: ObjectStore) -> dict[str, str]:
    """Each check is "ok" or a short reason; the details go to the log only."""
    checks: dict[str, str] = {}
    try:
        async with db.system() as conn:
            row = await fetch_one(
                conn,
                """SELECT NOT pg_is_in_recovery() AS writable,
                          to_regclass('public.tasks') IS NOT NULL AS migrated""",
            )
        assert row is not None
        checks["database"] = (
            "ok"
            if row["writable"] and row["migrated"]
            else "read_only"
            if not row["writable"]
            else "not_migrated"
        )
    except Exception:
        log.exception("readiness: database")
        checks["database"] = "unreachable"
    key = f"health/{uuid4()}"
    try:
        await objects.put(key, PROBE, "application/octet-stream")
        checks["objects"] = "ok" if await objects.get(key) == PROBE else "read_mismatch"
        await objects.delete(key)
    except Exception:
        log.exception("readiness: object store")
        checks["objects"] = "unreachable"
    return checks
