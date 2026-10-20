"""`witx admin recover`: make a database restored from a backup safe to run .

1. Replay the restore ledger (``services.ledger``): tasks deleted after the backup are
   deleted again; Gateways and devices get the state they last had, so nothing revoked
   or disabled since comes back.
2. Stop what was in flight: unfinished tasks stop, unsent input and its approval are
   cancelled, input sent without a result becomes unknown, so its device stays isolated
   until a person settles it from a new screenshot; leases are released, device epochs
   jump past anything issued after the backup, and everyone signs in again.

Run it while the API and the workers are stopped (``deploy/restore.sh`` does): it marks
every device offline, and only a Gateway's next connection brings them back. Running it
again replays nothing new; it only raises the device epochs once more, which is harmless.
"""

from datetime import datetime
from typing import Any

from witx.objects import ObjectStore
from witx.services import ledger
from witx.store import devices, recovery, retention
from witx.store.db import Database


async def recover(db: Database, objects: ObjectStore, now: datetime) -> dict[str, Any]:
    replayed = {"tasks_deleted": 0, "gateways": 0, "devices": 0}
    entries = await ledger.entries(objects)
    async with db.system() as conn:
        for kind, entity, state in entries:
            by = state.get("by") if await recovery.user_exists(conn, state.get("by")) else None
            if kind == "task" and await recovery.task_to_delete(conn, entity):
                audit = await retention.audit_summary(conn, entity)
                await retention.mark_deleted(conn, entity, user_id=by, audit=audit, now=now)
                replayed["tasks_deleted"] += 1
            elif kind == "gateway":
                current = await recovery.gateway_state(conn, entity)
                if current is None or current == state["state"] or current == "revoked":
                    continue
                if state["state"] == "revoked":
                    await devices.revoke_gateway(conn, entity)
                else:
                    await devices.set_gateway_disabled(conn, entity, state["state"] == "disabled")
                replayed["gateways"] += 1
            elif kind == "device":
                current_device = await recovery.device_state(conn, entity)
                if current_device is None or current_device["enabled"] == state["enabled"]:
                    continue
                await devices.set_enabled(
                    conn, entity, user_id=by, enabled=state["enabled"], name=state["name"]
                )
                replayed["devices"] += 1
        stopped = await recovery.stop_unfinished(conn, now)
    return replayed | stopped
