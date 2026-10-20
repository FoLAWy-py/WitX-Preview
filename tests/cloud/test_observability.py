"""Request logs, correlated logs from the worker, and /metrics ."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator
from witx.config import Settings
from witx.objects import LocalObjectStore, ObjectStore
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete, tap
from tests.conftest import Databases

TOKEN = "dev-token-acme"
METRICS = "metrics-s3cret"
Lines = Callable[[], list[dict[str, Any]]]


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


def client(
    rig: Rig, databases: Databases, objects: ObjectStore | None = None, **settings: Any
) -> httpx.AsyncClient:
    config = Settings(
        database_url=databases.app_url,
        object_store_url="file:///unused",
        dev_tokens={TOKEN: rig.tenant.subject},
        **settings,
    )
    app = create_app(
        config,
        db=rig.db,
        objects=objects or rig.objects,
        authenticator=DevAuthenticator(config.dev_tokens),
        hub=rig.hub,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture
async def web(rig: Rig, databases: Databases) -> AsyncIterator[httpx.AsyncClient]:
    async with client(rig, databases, metrics_token=METRICS) as c:
        yield c


async def finish_a_task(rig: Rig) -> str:
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device), complete]
    task = await rig.create_task()
    await rig.settle()
    return str(task.id)


async def test_a_request_is_logged_by_route_with_its_ids_and_never_with_its_query(
    rig: Rig, web: httpx.AsyncClient, log_lines: Lines
) -> None:
    task_id = await finish_a_task(rig)
    response = await web.get(
        f"/api/v1/tasks/{task_id}",
        params={"code": "one-time-code"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    (line,) = [entry for entry in log_lines() if entry["msg"] == "request"]
    assert line["route"] == "/api/v1/tasks/{task_id}" and line["status"] == 200
    assert line["task_id"] == task_id and line["tenant_id"] == str(rig.tenant.id)
    assert "one-time-code" not in str(log_lines())


async def test_what_the_worker_logs_is_tied_to_its_tenant_task_run_and_step(
    rig: Rig, log_lines: Lines
) -> None:
    task_id = await finish_a_task(rig)
    calls = [entry for entry in log_lines() if entry["msg"] == "model call"]
    assert len(calls) == 3
    for entry in calls:
        assert entry["tenant_id"] == str(rig.tenant.id) and entry["task_id"] == task_id
        assert entry["run_id"] and entry["step_id"] and "duration_ms" in entry


async def test_metrics_need_their_own_token(rig: Rig, databases: Databases) -> None:
    async with client(rig, databases) as off:
        assert (await off.get("/metrics")).status_code == 404
    async with client(rig, databases, metrics_token=METRICS) as on:
        assert (await on.get("/metrics")).status_code == 401
        user = {"Authorization": f"Bearer {TOKEN}"}
        assert (await on.get("/metrics", headers=user)).status_code == 401


async def test_metrics_report_outcomes_effects_latency_and_devices_per_tenant(
    rig: Rig, web: httpx.AsyncClient
) -> None:
    await finish_a_task(rig)
    response = await web.get("/metrics", headers={"Authorization": f"Bearer {METRICS}"})
    assert response.status_code == 200
    body = response.text
    for line in (
        'witx_tasks{tenant="acme",status="completed"} 1',
        'witx_tasks_finished_24h{tenant="acme",status="completed"} 1',
        'witx_actions_24h{tenant="acme",kind="tap",effect="unverified"} 1',
        'witx_actions_unknown_open{tenant="acme"} 0',
        'witx_devices{tenant="acme",platform="android",online="true"} 1',
    ):
        assert line in body, line
    assert 'witx_step_seconds_24h{tenant="acme",quantile="0.9"}' in body
    assert "# TYPE witx_run_queue_seconds gauge" in body


class FullDisk(LocalObjectStore):
    async def put(self, key: str, data: bytes, content_type: str) -> None:
        raise OSError("No space left on device")


async def test_the_api_is_ready_only_while_its_database_and_object_store_work(
    rig: Rig, databases: Databases, tmp_path: Path
) -> None:
    async with client(rig, databases) as web:
        ready = await web.get("/health/ready")
    assert ready.status_code == 200 and ready.json() == {"database": "ok", "objects": "ok"}
    async with client(rig, databases, objects=FullDisk(tmp_path)) as web:
        broken = await web.get("/health/ready")
    assert broken.status_code == 503 and broken.json()["objects"] == "unreachable"
