"""Liveness, readiness and metrics."""

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, Response

from witx.api.deps import (
    Deps,
)
from witx.services import health as readiness_checks
from witx.services import metrics

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready")
    async def ready() -> JSONResponse:
        """For the load balancer and the container runtime: 503 until the database is
        writable and migrated and the object store reads back what it stored."""
        checks = await readiness_checks.readiness(d.db, d.objects)
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(checks, status_code=200 if ok else 503)

    @router.get("/metrics")
    async def prometheus(authorization: Annotated[str | None, Header()] = None) -> Response:
        """Prometheus metrics, for the monitoring system only (WITX_METRICS_TOKEN)."""
        if not d.settings.metrics_token:
            raise HTTPException(404, "metrics are off")
        expected = f"Bearer {d.settings.metrics_token}".encode()
        if not hmac.compare_digest((authorization or "").encode(), expected):
            raise HTTPException(401, "metrics token required")
        return Response(
            await metrics.render(d.db), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    # ------------------------------------------------------------ sign-in

    return router
