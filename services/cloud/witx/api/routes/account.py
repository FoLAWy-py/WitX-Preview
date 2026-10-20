"""Signing in and out, the signed-in person, tenant settings and the model catalog."""

import logging
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from witx_contracts import api

from witx.api.auth import LoginFailed
from witx.api.deps import (
    LOGIN_COOKIE,
    LOGIN_SECONDS,
    SESSION_COOKIE,
    Deps,
)
from witx.domain import rules
from witx.domain.rules import Rejected
from witx.services import identity
from witx.services.identity import Actor
from witx.store import identity as identity_store
from witx.store import sessions

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/auth/methods")
    async def auth_methods() -> api.AuthMethods:
        return api.AuthMethods(oidc=d.login is not None, dev=d.settings.auth == "dev")

    @router.post("/api/v1/auth/dev-login", status_code=204)
    async def dev_login(body: api.DevLogin, response: Response) -> None:
        """Development only: a configured developer token becomes a browser session."""
        if d.settings.auth != "dev":
            raise HTTPException(404, "developer sign-in is off")
        subject = await d.authenticator.subject(body.token)
        if subject is None:
            raise HTTPException(401, "invalid token")
        await d.start_session(response, subject)

    @router.get("/api/v1/auth/login")
    async def oidc_login(return_to: str = "/") -> Response:
        if d.login is None:
            raise HTTPException(404, "OIDC sign-in is not configured")
        secret = d.login.new_secret()
        response = RedirectResponse(await d.login.authorization_url(secret), status_code=302)
        response.set_cookie(
            LOGIN_COOKIE,
            f"{secret}|{quote(rules.safe_return_path(return_to), safe='')}",
            max_age=LOGIN_SECONDS,
            httponly=True,
            secure=d.secure_cookies,
            samesite="lax",  # sent on the provider's redirect back to us
            path="/api/v1/auth",
        )
        return response

    @router.get("/api/v1/auth/callback")
    async def oidc_callback(
        code: str | None = None,
        state: str | None = None,
        pending: Annotated[str | None, Cookie(alias=LOGIN_COOKIE)] = None,
    ) -> Response:
        if d.login is None:
            raise HTTPException(404, "OIDC sign-in is not configured")

        def back(target: str) -> Response:
            response = RedirectResponse(f"{d.settings.public_url}{target}", status_code=302)
            response.delete_cookie(LOGIN_COOKIE, path="/api/v1/auth", secure=d.secure_cookies)
            return response

        secret, _, encoded = (pending or "").partition("|")
        if not code or not state or not secret:
            return back("/sign-in?error=not_completed")
        try:
            subject = await d.login.subject(code=code, state=state, secret=secret)
        except (LoginFailed, httpx.HTTPError, KeyError, ValueError):
            return back("/sign-in?error=not_completed")
        response = back(rules.safe_return_path(unquote(encoded)))
        try:
            await d.start_session(response, subject)
        except Rejected:
            return back("/sign-in?error=not_invited")
        return response

    @router.post("/api/v1/auth/logout", status_code=204)
    async def logout(
        request: Request,
        response: Response,
        x_csrf_token: Annotated[str | None, Header()] = None,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> None:
        if session is not None:
            try:
                await d.session_subject(
                    session, "POST", request.headers.get("origin"), x_csrf_token
                )
            except HTTPException as error:
                if error.status_code != 401:  # an expired session just loses its cookies
                    raise
            else:
                async with d.db.system() as conn:
                    await sessions.revoke(conn, session, datetime.now(UTC))
        d.end_session_cookies(response)

    @router.get("/api/v1/me")
    async def me(who: CurrentActor) -> api.Me:
        async with d.db.system() as conn:
            allow_full = await identity_store.allows_full_access(conn, who.tenant_id)
            fast = await identity_store.fast_risk_check(conn, who.tenant_id)
            providers = await d.allowed_providers(conn, who.tenant_id)
            fallback = await identity_store.model_fallback(conn, who.tenant_id)
            auto_memory = await identity_store.auto_memory(conn, who.tenant_id, who.user_id)
            chosen = await identity_store.screenshot_retention_days(
                conn, who.tenant_id, who.user_id
            )
        most = rules.retention_most(d.settings.screenshot_retention.days)
        chosen = min(chosen, most) if chosen else None
        return api.Me(
            user_id=who.user_id,
            display_name=who.display_name,
            tenant_id=who.tenant_id,
            tenant_name=who.tenant_name,
            role="owner" if who.role == "owner" else "member",
            allow_full_access=allow_full,
            fast_risk_check=fast,
            allowed_providers=sorted(providers),
            model_fallback=fallback,
            auto_memory=auto_memory,
            screenshot_retention_days=chosen or most,
            screenshot_retention_max=most,
            audit_retention_days=d.settings.audit_retention.days,
        )

    @router.put("/api/v1/me/retention")
    async def set_retention(body: api.RetentionSetting, who: CurrentActor) -> api.RetentionSetting:
        """How long the screenshots of the signed-in person's finished tasks are kept,
        up to the deployment's most."""
        most = rules.retention_most(d.settings.screenshot_retention.days)
        if body.screenshot_retention_days > most:
            raise Rejected("invalid_arguments", f"at most {most} days here")
        async with d.db.system() as conn:
            await identity_store.set_screenshot_retention_days(
                conn, who.tenant_id, who.user_id, body.screenshot_retention_days
            )
        return body

    @router.put("/api/v1/tenant/settings")
    async def tenant_settings(body: api.TenantSettings, who: CurrentActor) -> api.TenantSettings:
        async with d.db.system() as conn:
            if body.allow_full_access is not None:
                await identity.set_full_access(conn, who, body.allow_full_access)
            if body.fast_risk_check is not None:
                await identity.set_fast_risk_check(conn, who, body.fast_risk_check)
            if body.model_fallback is not None:
                await identity.set_model_fallback(conn, who, body.model_fallback)
            if body.allowed_providers is not None:
                await identity.set_allowed_providers(
                    conn,
                    who,
                    body.allowed_providers,
                    {m.provider for m in d.settings.catalog().values()},
                )
            return api.TenantSettings(
                allow_full_access=await identity_store.allows_full_access(conn, who.tenant_id),
                fast_risk_check=await identity_store.fast_risk_check(conn, who.tenant_id),
                allowed_providers=sorted(await d.allowed_providers(conn, who.tenant_id)),
                model_fallback=await identity_store.model_fallback(conn, who.tenant_id),
            )

    @router.get("/api/v1/models")
    async def list_models(who: CurrentActor) -> list[api.ModelView]:
        async with d.db.system() as conn:
            allowed = await d.allowed_providers(conn, who.tenant_id)
        return [
            api.ModelView(
                name=m.name,
                provider=m.provider,
                default=m.name == d.settings.default_model,
                allowed=m.provider in allowed,
                efforts=list(m.efforts),
            )
            for m in d.settings.catalog().values()
        ]

    return router
