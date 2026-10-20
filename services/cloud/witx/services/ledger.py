"""What a database restore must not undo : deleted tasks, and changes
to who may act (Gateways disabled, revoked or enabled again; devices enabled or not).

Each change is also written, before it is made, to the object store under ``ledger/``:
one object per task, Gateway or device, holding its latest state. Restoring the database
from a backup does not touch these objects (the backup and restore scripts leave
``ledger/`` out), so ``witx admin recover`` can replay them onto the restored database.
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from witx.objects import ObjectStore

PREFIX = "ledger/"


async def record(objects: ObjectStore, kind: str, entity: UUID, state: dict[str, Any]) -> None:
    """Writes ``kind``'s latest state for ``entity``: task, gateway or device."""
    body = {**state, "at": datetime.now(UTC).isoformat()}
    await objects.put(
        f"{PREFIX}{kind}/{entity}", json.dumps(body, default=str).encode(), "application/json"
    )


async def entries(objects: ObjectStore) -> list[tuple[str, UUID, dict[str, Any]]]:
    """Every recorded state, as (kind, entity, state)."""
    found = []
    for key in await objects.list(PREFIX):
        kind, _, entity = key.removeprefix(PREFIX).partition("/")
        found.append((kind, UUID(entity), json.loads(await objects.get(key))))
    return found
