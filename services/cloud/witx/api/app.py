"""HTTP and WebSocket entry points.

``/api/v1`` serves the Web app (bearer user tokens). ``/gateway/v1`` serves Gateways
(bearer Gateway tokens). Creating the app has no side effects; connections are made
on first use.
"""

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from witx.api import mcp
from witx.api.auth import Authenticator, OidcLogin
from witx.api.deps import Deps
from witx.api.requests import RequestLog
from witx.api.routes import (
    account,
    actions,
    devices,
    gateway,
    memories,
    schedules,
    side_chats,
    skills,
    system,
    takeover,
    tasks,
    workspace,
)
from witx.config import Settings, plugins_dir
from witx.domain.rules import Rejected
from witx.harness.plugins import Installed
from witx.hub import Hub
from witx.objects import ObjectStore
from witx.store.db import Database

log = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    *,
    db: Database,
    objects: ObjectStore,
    authenticator: Authenticator,
    hub: Hub,
    login: OidcLogin | None = None,
    plugins: Installed | None = None,
) -> FastAPI:
    # MCP clients are served under /api/v1/mcp by the same process.
    mcp_server = mcp.build(settings, db=db, objects=objects, authenticator=authenticator)
    mcp_app = mcp.app(mcp_server)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield
        await d.workspace.close()

    app = FastAPI(
        title="WitX",
        version="0.2.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Witx-Tenant",
                "X-CSRF-Token",
                "Last-Event-ID",
                "Idempotency-Key",
            ],
        )

    @app.exception_handler(Rejected)
    async def rejected(_: Request, error: Rejected) -> JSONResponse:
        status = 404 if error.code in ("not_found", "unknown_device") else 409
        if error.code in (
            "unknown_user",
            "no_membership",
            "tenant_required",
            "owner_required",
            "full_access_not_allowed",
            "invalid_code",
            "web_approval_required",
            "provider_not_allowed",
        ):
            status = 403
        if error.code in (
            "unknown_model",
            "invalid_effort",
            "invalid_schedule",
            "unknown_timezone",
            "invalid_providers",
            "invalid_memory",
            "memory_not_allowed",
            "invalid_skill",
            "skill_not_allowed",
            "plugin_not_fit",
        ):
            status = 422
        if error.code == "marketplace_unavailable":
            status = 502
        return JSONResponse({"code": error.code, "detail": error.detail}, status_code=status)

    d = Deps(settings, db, objects, authenticator, hub, login)
    app.include_router(system.routes(d))
    app.include_router(account.routes(d))
    app.include_router(devices.routes(d))
    app.include_router(tasks.routes(d))
    app.include_router(takeover.routes(d))
    app.include_router(schedules.routes(d))
    app.include_router(side_chats.routes(d))
    app.include_router(actions.routes(d))
    app.include_router(memories.routes(d))
    app.include_router(skills.routes(d, plugins or Installed(plugins_dir())))
    app.include_router(gateway.routes(d))
    app.include_router(workspace.routes(d))
    app.mount("/", mcp_app)  # last: only paths no route above matched reach it
    app.add_middleware(RequestLog)  # outermost: sees the final status
    return app
