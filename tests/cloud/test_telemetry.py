"""Optional tracing: off unless an OTLP endpoint is set; spans carry correlation
IDs, never what the user wrote."""

from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from witx import telemetry
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import device_info, provision
from tests.cloud.test_flow import complete

SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)


def test_tracing_is_off_unless_an_endpoint_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.configure("worker") is False


async def test_an_agent_step_is_traced_with_its_ids_and_not_what_the_user_wrote(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    rig.model.script = [complete]
    SPANS.clear()
    task = await rig.create_task("Send my secret plan to Bob")
    await rig.settle()

    spans = {s.name: s for s in SPANS.get_finished_spans()}
    assert {"agent.step", "agent.model_call"} <= set(spans)
    call = spans["agent.model_call"].attributes or {}
    assert call["witx.task_id"] == str(task.id) and call["witx.model"] == "test-model"
    written = " ".join(
        str(v) for s in SPANS.get_finished_spans() for v in (s.attributes or {}).values()
    )
    assert "secret plan" not in written
