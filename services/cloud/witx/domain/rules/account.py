"""Access: browser sessions, Gateway identity, idempotency keys and retention."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from witx.domain.rules.common import Rejected

# How long a write request's Idempotency-Key is remembered.
IDEMPOTENCY_TTL = timedelta(days=1)


# The most days a person may keep their finished tasks' screenshots.
SCREENSHOT_RETENTION_MOST = 30


def retention_most(deployment_days: int) -> int:
    """The most a person may choose: 30 days, or fewer where the deployment keeps
    screenshots for less."""
    return max(1, min(SCREENSHOT_RETENTION_MOST, deployment_days))


def request_digest(content: Mapping[str, Any]) -> str:
    """SHA-256 of a request's content, independent of key order and spacing."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_key_reuse(*, stored_request: str, stored_digest: str, request: str, digest: str) -> None:
    """An Idempotency-Key may only be sent again with the same request and content."""
    if (stored_request, stored_digest) != (request, digest):
        raise Rejected(
            "idempotency_key_reused", "this Idempotency-Key was sent with another request"
        )


# A session ends after this long without a request, and in any case after SESSION_MAX.
SESSION_IDLE = timedelta(hours=12)


SESSION_MAX = timedelta(days=7)


# Requests that change nothing; every other cookie-authenticated request needs the CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def session_valid(
    *, seen_at: datetime, expires_at: datetime, revoked_at: datetime | None, now: datetime
) -> bool:
    return revoked_at is None and now < expires_at and now - seen_at < SESSION_IDLE


def safe_return_path(path: str | None) -> str:
    """Where to go after signing in: only a path on this site (never another host)."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


# How long a one-time enrollment code can be used.
ENROLL_CODE_TTL = timedelta(hours=24)


# A Gateway asks for its next credential once its current one is this old.
CREDENTIAL_ROTATE_AFTER = timedelta(days=30)


def credential_due(issued_at: datetime | None, now: datetime) -> bool:
    return issued_at is None or now - issued_at >= CREDENTIAL_ROTATE_AFTER


def parse_enrollment_code(code: str) -> tuple[UUID, str]:
    """An enrollment code is "<tenant id>.<secret>": the tenant lets the cloud look the
    code up under that tenant's row-level security, with no cross-tenant query."""
    tenant, _, secret = code.strip().partition(".")
    try:
        tenant_id = UUID(tenant)
    except ValueError:
        raise Rejected("invalid_code") from None
    if len(secret) < 32:
        raise Rejected("invalid_code")
    return tenant_id, secret
