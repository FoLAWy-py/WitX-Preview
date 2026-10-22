"""What every group of API routes shares: the process's services, how a request is
authenticated, browser sessions, Idempotency-Key handling and the model checks."""

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import Cookie, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection
from witx_contracts import logs

from witx.api.auth import Authenticator, OidcLogin
from witx.api.workspace import Workspace
from witx.config import Settings
from witx.domain import rules
from witx.domain.rules import Rejected
from witx.hub import Hub
from witx.objects import ObjectStore
from witx.services import identity
from witx.services.identity import Actor
from witx.store import idempotency, sessions
from witx.store import identity as identity_store
from witx.store.db import Database

Response_ = TypeVar("Response_", bound=BaseModel)

# Browser session cookies. The session token is HttpOnly; the CSRF token
# is readable by the page, which echoes it in X-CSRF-Token on every write.
SESSION_COOKIE = "witx_session"
CSRF_COOKIE = "witx_csrf"
LOGIN_COOKIE = "witx_login"  # an OIDC sign-in in progress
LOGIN_SECONDS = 600
TOUCH_EVERY = timedelta(minutes=5)

IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=r"^[!-~]+$"),
]


def bearer(value: str | None) -> str:
    if not value or not value.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return value[7:].strip()


@dataclass
class Deps:
    settings: Settings
    db: Database
    objects: ObjectStore
    authenticator: Authenticator
    hub: Hub
    login: OidcLogin | None = None
    secure_cookies: bool = field(init=False)
    workspace: Workspace = field(init=False)
    allowed_origins: set[str] = field(init=False)
    default_provider: str = field(init=False)

    def __post_init__(self) -> None:
        self.secure_cookies = self.settings.public_url.startswith("https://")
        self.workspace = Workspace(self.settings.database_url)
        self.allowed_origins = {self.settings.public_url, *self.settings.cors_origins}
        self.default_provider = self.settings.catalog()[self.settings.default_model].provider

    # ------------------------------------------------------------ who is asking

    async def bearer_subject(self, authorization: str) -> str:
        """Programs authenticate with a bearer token, which browsers never send on their own."""
        subject = await self.authenticator.subject(bearer(authorization))
        if subject is None:
            raise HTTPException(401, "invalid token")
        return subject

    async def session_subject(
        self, token: str, method: str, origin: str | None, csrf: str | None
    ) -> str:
        """Browsers authenticate with the session cookie. A write must also come from one of
        our own pages and carry the session's CSRF token."""
        now = datetime.now(UTC)
        async with self.db.system() as conn:
            session = await sessions.get(conn, token, now)
            if session is None:
                raise HTTPException(401, "not signed in")
            if method not in rules.SAFE_METHODS:
                if origin is not None and origin not in self.allowed_origins:
                    raise HTTPException(403, "request from another site")
                if csrf is None or not hmac.compare_digest(csrf, session.csrf):
                    raise HTTPException(403, "missing or wrong CSRF token")
            if now - session.seen_at > TOUCH_EVERY:
                await sessions.touch(conn, token, now)
        return session.subject

    async def actor(
        self,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_witx_tenant: Annotated[UUID | None, Header()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> Actor:
        if authorization is not None:
            subject = await self.bearer_subject(authorization)
        elif session is not None:
            subject = await self.session_subject(
                session, request.method, request.headers.get("origin"), x_csrf_token
            )
        else:
            raise HTTPException(401, "not signed in")
        async with self.db.system() as conn:
            who = await identity.actor_for(conn, subject, x_witx_tenant)
        logs.bind(tenant_id=who.tenant_id, user_id=who.user_id)
        return who

    # ------------------------------------------------------------ browser sessions

    async def start_session(self, response: Response, subject: str) -> None:
        """Signs a known user in (invitation only: unknown subjects are refused)."""
        now = datetime.now(UTC)
        async with self.db.system() as conn:
            user = await identity_store.user_by_subject(conn, subject)
            if user is None:
                raise Rejected("unknown_user")
            token, csrf = await sessions.create(conn, user_id=user["id"], subject=subject, now=now)
        age = int(rules.SESSION_MAX.total_seconds())
        for name, value, httponly in ((SESSION_COOKIE, token, True), (CSRF_COOKIE, csrf, False)):
            response.set_cookie(
                name,
                value,
                max_age=age,
                httponly=httponly,
                secure=self.secure_cookies,
                samesite="lax",
                path="/",
            )

    def end_session_cookies(self, response: Response) -> None:
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            response.delete_cookie(name, path="/", secure=self.secure_cookies, samesite="lax")

    # ------------------------------------------------------------ writes

    async def once(
        self,
        conn: AsyncConnection,
        who: Actor,
        key: str | None,
        request: Request,
        content: BaseModel | None,
        model: type[Response_],
        write: Callable[[], Awaitable[Response_]],
    ) -> Response_:
        """Runs a write once per Idempotency-Key, inside the caller's transaction.

        A retry with the same key, request and content gets the stored response; the
        same key with another request is refused. A failed write stores nothing, so a
        retry runs it again. Without a key the write simply runs.
        """
        if key is None:
            return await write()
        name = f"{request.method} {request.url.path}"
        digest = rules.request_digest(content.model_dump(mode="json") if content else {})
        await idempotency.lock(conn, who.tenant_id, key)
        stored = await idempotency.get(conn, key, since=datetime.now(UTC) - rules.IDEMPOTENCY_TTL)
        if stored is not None:
            rules.check_key_reuse(
                stored_request=stored.request,
                stored_digest=stored.digest,
                request=name,
                digest=digest,
            )
            if stored.response is None:
                raise Rejected("not_found", "what this request was about has been deleted")
            return model.model_validate(stored.response)
        result = await write()
        await idempotency.put(
            conn,
            tenant_id=who.tenant_id,
            key=key,
            user_id=who.user_id,
            request=name,
            digest=digest,
            response=result.model_dump(mode="json"),
        )
        return result

    # ------------------------------------------------------------ models

    async def allowed_providers(self, conn: AsyncConnection, tenant_id: UUID) -> frozenset[str]:
        chosen = await identity_store.allowed_providers(conn, tenant_id)
        return rules.allowed_providers(chosen, self.default_provider)

    async def check_model(
        self, conn: AsyncConnection, tenant_id: UUID, model: str, effort: str | None = None
    ) -> None:
        """A model a task may use: in the catalog, from a provider the tenant allows, at an
        effort it goes down to."""
        rules.check_model(model, self.settings.catalog())
        chosen = self.settings.catalog()[model]
        allowed = await self.allowed_providers(conn, tenant_id)
        rules.check_provider(chosen.provider, allowed)
        if effort is not None:
            rules.check_reasoning_effort(effort, chosen.lowest_effort)
