"""The whole system over real sockets: HTTP API + WebSocket hub + worker + Gateway.

Only the model is scripted and the phone is the in-memory fake device.
"""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator
from witx.config import Settings
from witx.harness.loop import Harness
from witx.hub import Hub
from witx.objects import LocalObjectStore
from witx.store.db import Database
from witx.worker import Worker
from witx_gateway.client import GatewayClient
from witx_gateway.devices.fake import FakeDevice
from witx_gateway.journal import Journal

from tests.cloud.rig import last_screenshot
from tests.cloud.support import FakeRisk, ScriptedModel, call, provision, say
from tests.conftest import Databases
from tests.gateway.keystore import stored

TOKEN = "dev-token-alice"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def system(
    databases: Databases, db: Database, admin_db: Database, tmp_path: Path
) -> AsyncIterator[tuple[str, ScriptedModel, FakeDevice, Worker]]:
    tenant = await provision(admin_db, "acme")
    settings = Settings(
        database_url=databases.app_url,
        object_store_url=f"file://{tmp_path}/objects",
        dev_tokens={TOKEN: tenant.subject},
    )
    objects = LocalObjectStore(tmp_path / "objects")
    hub = Hub(db, objects, settings.limits, poll_seconds=0.05)
    app = create_app(
        settings,
        db=db,
        objects=objects,
        authenticator=DevAuthenticator(settings.dev_tokens),
        hub=hub,
    )
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)

    # Small enough to be sent unscaled, so model pixels are device points.
    device = FakeDevice(native_id="emulator-5554", width=720, height=1600)
    gateway = GatewayClient(
        cloud_url=f"http://127.0.0.1:{port}",
        credentials=stored(tmp_path / "journal.sqlite3", tenant.gateway_token),
        journal=Journal(tmp_path / "journal.sqlite3"),
        devices=[device],
    )
    stop = asyncio.Event()
    connecting = asyncio.create_task(gateway.run(stop))
    await asyncio.wait_for(gateway.connected.wait(), 5)

    model = ScriptedModel([])
    worker = Worker(db, Harness(db, model, objects, settings.limits, FakeRisk()))  # type: ignore[arg-type]
    try:
        yield f"http://127.0.0.1:{port}", model, device, worker
    finally:
        stop.set()
        await connecting
        server.should_exit = True
        await serving


async def drive(
    worker: Worker, until: "asyncio.Future[bool] | None" = None, seconds: float = 10
) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        await worker.run_once()
        await asyncio.sleep(0.05)
        if until is not None and until.done():
            return


async def test_a_task_runs_from_the_web_api_to_the_phone_and_back(system) -> None:  # type: ignore[no-untyped-def]
    base, model, device, worker = system
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(base_url=base, headers=auth) as web:
        (phone,) = (await web.get("/api/v1/devices")).json()
        assert phone["online"] and phone["platform"] == "fake"
        assert not phone["enabled"]  # a device the Gateway just reported waits for an owner
        device_id = phone["id"]
        enabled = await web.post(f"/api/v1/devices/{device_id}/enable", json={"name": "Test phone"})
        assert enabled.status_code == 200 and enabled.json()["enabled"]
        model.script = [
            [call("observe", device_id=device_id)],
            lambda r: [
                call(
                    "tap",
                    device_id=device_id,
                    observation_id=last_screenshot(r),
                    x=100,
                    y=200,
                    target="button",
                    expected_effect="opens",
                    effects=[],
                )
            ],
            lambda r: [
                call("complete_task", summary="done", evidence_observation_ids=[last_screenshot(r)])
            ],
        ]
        created = await web.post(
            "/api/v1/tasks", json={"goal": "Tap the button", "device_ids": [device_id]}
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        async def watch() -> None:
            while True:
                if (await web.get(f"/api/v1/tasks/{task_id}")).json()["status"] == "completed":
                    done.set_result(True)
                    return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch())
        await drive(worker, done)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher

        detail = (await web.get(f"/api/v1/tasks/{task_id}")).json()
        assert detail["status"] == "completed"
        assert device.inputs == [("tap", (100.0, 200.0))]
        (action,) = detail["actions"]
        assert action["status"] == "done" and action["effect"] == "unverified"
        assert action["finished_at"] >= action["created_at"]  # how long the step took

        evidence = detail["result"]["evidence_observation_ids"][0]
        image = await web.get(f"/api/v1/observations/{evidence}/screenshot")
        assert image.headers["content-type"] == "image/png" and image.content[:4] == b"\x89PNG"
        thumb = await web.get(f"/api/v1/observations/{evidence}/screenshot?width=120")
        assert thumb.headers["content-type"] == "image/jpeg"  # a thumbnail for the page

        async with web.stream("GET", f"/api/v1/tasks/{task_id}/events") as stream:
            lines = []
            async for line in stream.aiter_lines():
                lines.append(line)
                if line.startswith("event: task.status"):
                    break
        assert lines[0] == "id: 1" and "event: task.created" in lines


async def frames(lines: AsyncIterator[str], until_seq: int) -> list[tuple[int, str]]:
    """The next events of an event stream's lines, as (seq, type), up to the one numbered
    ``until_seq``."""
    seen: list[tuple[int, str]] = []
    seq = 0
    async for line in lines:
        if line.startswith("id: "):
            seq = int(line[4:])
        elif line.startswith("event: ") and line != "event: agent.saying":
            seen.append((seq, line[7:]))
            if seq >= until_seq:
                break
    return seen


async def test_a_replay_holds_only_the_kinds_asked_for_and_every_later_event(system) -> None:  # type: ignore[no-untyped-def]
    base, model, _, worker = system
    model.script = [
        [say("Hello."), call("complete_task", summary="done", evidence_observation_ids=[])]
    ]
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(base_url=base, headers=auth, timeout=10) as web:
        created = await web.post("/api/v1/tasks", json={"goal": "Say hello", "device_ids": []})
        task_id = created.json()["id"]
        for _ in range(100):
            await worker.run_once()
            if (await web.get(f"/api/v1/tasks/{task_id}")).json()["status"] == "completed":
                break
        head = (await web.get(f"/api/v1/tasks/{task_id}")).json()["last_event_seq"]
        async with web.stream("GET", f"/api/v1/tasks/{task_id}/events") as stream:
            everything = await frames(stream.aiter_lines(), head)
        wanted = {"task.created", "agent.message"}
        kept = [(seq, kind) for seq, kind in everything if kind in wanted]
        assert len(kept) == 2 and len(everything) > len(kept)

        url = f"/api/v1/tasks/{task_id}/events?types=task.created,agent.message"
        async with web.stream("GET", url) as stream:
            lines = stream.aiter_lines()
            assert await frames(lines, kept[-1][0]) == kept
            # A message sent now is an event the replay would have left out: it comes.
            sent = await web.post(f"/api/v1/tasks/{task_id}/inputs", json={"text": "Again"})
            assert sent.status_code == 200
            later = await frames(lines, head + 1)
        assert (head + 1, "input.added") in later


async def test_a_workspace_page_hears_of_a_task_made_and_what_the_worker_does_with_it(  # type: ignore[no-untyped-def]
    system,
) -> None:
    base, model, _, worker = system
    model.script = [
        [say("Hello."), call("complete_task", summary="done", evidence_observation_ids=[])]
    ]
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with (
        httpx.AsyncClient(base_url=base, headers=auth, timeout=10) as web,
        web.stream("GET", "/api/v1/workspace/events") as stream,
    ):
        lines = stream.aiter_lines()
        assert await anext(lines) == ": listening"  # from here on nothing is missed

        created = await web.post("/api/v1/tasks", json={"goal": "Say hello", "device_ids": []})
        task_id = created.json()["id"]
        heard: list[tuple[str, str | None]] = []
        while ("task", "task.created") not in heard:
            line = await anext(lines)
            if line.startswith("data: "):
                event = json.loads(line[6:])
                assert event["data"]["task_id"] == task_id
                heard.append((event["type"], event["data"]["type"]))

        for _ in range(20):  # the worker, in its own transactions
            await worker.run_once()
        while ("task", "agent.message") not in heard:
            line = await anext(lines)
            if line.startswith("data: "):
                event = json.loads(line[6:])
                heard.append((event["type"], event["data"]["type"]))


async def test_requests_need_a_valid_token(system) -> None:  # type: ignore[no-untyped-def]
    base, *_ = system
    async with httpx.AsyncClient(base_url=base) as web:
        assert (await web.get("/api/v1/tasks")).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert (await web.get("/api/v1/tasks", headers=bad)).status_code == 401
        upload = await web.put(f"/gateway/v1/observations/{'0' * 32}/screenshot", headers=bad)
        assert upload.status_code in (401, 422)
