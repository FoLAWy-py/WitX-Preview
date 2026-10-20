"""Who is calling: maps an authenticated subject to a user and one tenant, and a
Gateway's machine credential to its Gateway ."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.domain.rules import Rejected
from witx.store import devices, identity
from witx.store.db import Database


@dataclass(frozen=True)
class Actor:
    user_id: UUID
    display_name: str
    tenant_id: UUID
    tenant_name: str
    role: str


async def actor_for(conn: AsyncConnection, subject: str, tenant_id: UUID | None) -> Actor:
    """Resolves the user's membership. With several tenants the caller must pick one."""
    user = await identity.user_by_subject(conn, subject)
    if user is None:
        raise Rejected("unknown_user")
    memberships = await identity.memberships(conn, user["id"])
    if tenant_id is not None:
        memberships = [m for m in memberships if m["tenant_id"] == tenant_id]
    if len(memberships) != 1:
        raise Rejected("tenant_required" if memberships else "no_membership")
    m = memberships[0]
    return Actor(
        user_id=user["id"],
        display_name=user["display_name"],
        tenant_id=m["tenant_id"],
        tenant_name=m["tenant_name"],
        role=m["role"],
    )


async def set_full_access(conn: AsyncConnection, actor: Actor, allowed: bool) -> None:
    """Only a tenant owner may let members choose "full access"."""
    if actor.role != "owner":
        raise Rejected("owner_required", "only a tenant owner can change this")
    await identity.set_allow_full_access(conn, actor.tenant_id, allowed)


async def set_fast_risk_check(conn: AsyncConnection, actor: Actor, on: bool) -> None:
    """Only a tenant owner may let the risk check try the Decisions API first."""
    if actor.role != "owner":
        raise Rejected("owner_required", "only a tenant owner can change this")
    await identity.set_fast_risk_check(conn, actor.tenant_id, on)


async def set_model_fallback(conn: AsyncConnection, actor: Actor, on: bool) -> None:
    """Only a tenant owner lets tasks switch models on their own ."""
    if actor.role != "owner":
        raise Rejected("owner_required", "only a tenant owner can change this")
    await identity.set_model_fallback(conn, actor.tenant_id, on)


async def set_allowed_providers(
    conn: AsyncConnection, actor: Actor, chosen: list[str], known: set[str]
) -> None:
    """Only a tenant owner chooses which model providers may receive its data."""
    if actor.role != "owner":
        raise Rejected("owner_required", "only a tenant owner can change this")
    picked = rules.check_provider_choice(chosen, known)
    await identity.set_allowed_providers(conn, actor.tenant_id, picked)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def gateway_for_token(conn: AsyncConnection, token: str) -> tuple[UUID, UUID] | None:
    """Returns (gateway_id, tenant_id) for a Gateway credential, the first use of a
    rotated credential included (which retires the previous one)."""
    return await devices.gateway_by_token(conn, token_hash(token))


def new_gateway_secret() -> str:
    return "wgw_" + secrets.token_urlsafe(32)


async def create_gateway(
    conn: AsyncConnection, *, tenant_id: UUID, name: str, now: datetime
) -> tuple[UUID, str]:
    """A Gateway waiting to enroll. Returns its ID and its one-time enrollment code."""
    secret = secrets.token_urlsafe(32)
    gateway_id = await devices.insert_gateway(
        conn,
        tenant_id=tenant_id,
        name=name,
        enroll_code_hash=token_hash(secret),
        enroll_expires_at=now + rules.ENROLL_CODE_TTL,
    )
    return gateway_id, f"{tenant_id}.{secret}"


async def enroll_gateway(db: Database, *, code: str, host: str, now: datetime) -> tuple[UUID, str]:
    """Spends an enrollment code for the Gateway's first credential."""
    tenant_id, secret = rules.parse_enrollment_code(code)
    credential = new_gateway_secret()
    async with db.tenant(tenant_id) as conn:
        gateway_id = await devices.enroll_gateway(
            conn,
            code_hash=token_hash(secret),
            token_hash=token_hash(credential),
            host=host[:255],
            now=now,
        )
    if gateway_id is None:
        raise Rejected("invalid_code", "unknown, used or expired enrollment code")
    return gateway_id, credential


async def rotate_gateway_credential(conn: AsyncConnection, gateway_id: UUID) -> str:
    """Issues the Gateway's next credential; the current one works until it is used."""
    credential = new_gateway_secret()
    await devices.issue_next_credential(conn, gateway_id, token_hash(credential))
    return credential
