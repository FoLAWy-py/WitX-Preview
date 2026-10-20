"""Optional tracing with OpenTelemetry. Off unless OTEL_EXPORTER_OTLP_ENDPOINT is
set; then spans go to that OTLP/HTTP endpoint (the other standard OTEL_* variables
apply). Spans carry the same correlation IDs as the logs (task, run, step), never input
text, request bodies or headers."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncEngine

_tracer = trace.get_tracer("witx")


def configure(
    service: str, *, app: FastAPI | None = None, engine: AsyncEngine | None = None
) -> bool:
    """Sends this process's spans to the configured endpoint; False when tracing is off."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": os.environ.get("OTEL_SERVICE_NAME", f"witx-{service}")}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # Outgoing HTTP: model providers and the object store; URLs only, no bodies.
    HTTPXClientInstrumentor().instrument()
    if engine is not None:
        # SQL with placeholders, never the values bound to them.
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """A span named ``name`` with correlation IDs as attributes (a no-op when off)."""
    with _tracer.start_as_current_span(
        name, attributes={f"witx.{k}": str(v) for k, v in attributes.items() if v is not None}
    ):
        yield
