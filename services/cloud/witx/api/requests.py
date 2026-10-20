"""One log line per HTTP request: the route template rather than the URL (query strings
carry OIDC codes and states), the IDs in the path, the status and the duration."""

import logging
import time
from secrets import token_hex
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from witx_contracts import logs

log = logging.getLogger("witx.requests")

QUIET = {"/health", "/health/ready", "/metrics"}  # polled by infrastructure: debug level only
# A LogRecord's own attributes (``name`` of /api/v1/skills/my/{name}) cannot be extra fields.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class RequestLog:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # sockets log their own lifecycle
            return
        started = time.monotonic()
        status = 500

        async def sending(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with logs.log_context(request_id=token_hex(8)):
            try:
                await self.app(scope, receive, sending)
            finally:
                route = getattr(scope.get("route"), "path", "unmatched")
                fields: dict[str, Any] = {
                    "method": scope["method"],
                    "route": route,
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    **{
                        f"path_{k}" if k in _RESERVED else k: v
                        for k, v in scope.get("path_params", {}).items()
                    },
                }
                level = logging.DEBUG if route in QUIET else logging.INFO
                log.log(level, "request", extra=fields)
