"""Test helpers: provisioning, a scripted model and an in-memory Gateway."""

import hashlib
import json
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from witx.domain.model import RiskVerdict
from witx.harness.risk import RiskCheck
from witx.models import ModelRequest, ModelResponse
from witx.services.identity import token_hash
from witx.store import devices as device_store
from witx.store import identity
from witx.store.db import Database


@dataclass(frozen=True)
class Tenant:
    id: UUID
    user_id: UUID
    subject: str
    gateway_id: UUID
    gateway_token: str
    device_ids: list[UUID]


async def provision(admin_db: Database, name: str, *, devices: int = 0) -> Tenant:
    count = devices
    subject = f"user-{name}"
    token = f"gateway-token-{name}"
    async with admin_db.system() as conn:
        tenant_id = await identity.ensure_tenant(conn, name)
        user_id = await identity.ensure_user(conn, subject, name.title())
        await identity.ensure_membership(conn, tenant_id, user_id)
        gateway_id = await device_store.insert_gateway(
            conn, tenant_id=tenant_id, name=f"{name}-gw", token_hash=token_hash(token)
        )
        registered = await device_store.sync_gateway_devices(
            conn,
            tenant_id=tenant_id,
            gateway_id=gateway_id,
            devices=[device_info(i) for i in range(count)],
        )
        for device in registered:  # the owner enabled them (device pool, #33)
            await device_store.set_enabled(
                conn, device.id, user_id=user_id, enabled=True, name=device.name
            )
    return Tenant(tenant_id, user_id, subject, gateway_id, token, [d.id for d in registered])


def device_info(i: int) -> dict[str, Any]:
    return {
        "native_id": f"emulator-{5554 + 2 * i}",
        "platform": "android",
        "name": f"Pixel {i}",
        "capabilities": ["tap", "swipe", "back", "home", "launch_app", "find_app"],
    }


def png(width: int, height: int) -> bytes:
    """A valid grey PNG of the given size."""
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- scripted model


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{uuid4().hex[:8]}",
        "call_id": f"call_{uuid4().hex[:8]}",
        "name": name,
        "arguments": json.dumps(arguments, default=str),
    }


def say(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": f"msg_{uuid4().hex[:8]}",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


@dataclass
class ScriptedModel:
    """Answers each request with the next scripted output. A script entry may be a
    function of the request, to refer to IDs that only exist at run time."""

    script: list[list[dict[str, Any]] | Callable[[ModelRequest], list[dict[str, Any]]]]
    requests: list[ModelRequest] = field(default_factory=list)

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        entry = self.script.pop(0)
        output = entry(request) if callable(entry) else entry
        return ModelResponse(
            id=f"resp_{len(self.requests)}",
            output=output,
            usage={"total_tokens": 1},
            raw={"output": output},
        )


HARMLESS = RiskVerdict(sensitive=False, effects=[], reason="opens a settings page")
RISK_USAGE = {"input_tokens": 700, "output_tokens": 20}


@dataclass
class FakeRisk:
    """Stands in for the independent risk assessor; returns ``verdict`` every time."""

    verdict: RiskVerdict = HARMLESS
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def assess(self, **kw: Any) -> RiskCheck:
        self.calls.append(kw)
        record = {"model": "fake-risk", "usage": RISK_USAGE, "duration_ms": 5, "error": None}
        return RiskCheck(self.verdict, record)
