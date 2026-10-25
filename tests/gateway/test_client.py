"""The Gateway's connection to the cloud."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from witx_gateway.client import GatewayClient, checked_cloud_url
from witx_gateway.journal import Journal

from tests.gateway.keystore import stored


async def test_after_a_connection_that_worked_a_drop_is_retried_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "journal.sqlite3"
    client = GatewayClient(
        cloud_url="http://127.0.0.1:1",
        credentials=stored(journal, "wgw_test"),
        journal=Journal(journal),
        devices=[],
    )
    stop = asyncio.Event()
    welcomed = [False, False, False, True, False]  # the 4th attempt connected, then dropped

    async def attempt(_: asyncio.Event) -> None:
        client._welcomed = welcomed.pop(0)
        if not welcomed:
            stop.set()
        raise OSError("connection reset")

    waits: list[float] = []

    async def wait_for(awaitable: Any, timeout: float) -> None:
        waits.append(timeout)
        awaitable.close()

    monkeypatch.setattr(client, "_session", attempt)
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    await client.run(stop)
    assert waits == [1, 2, 4, 1, 2]


async def test_a_plugged_in_or_unplugged_device_is_reported_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from witx_contracts import gateway as wire

    journal = tmp_path / "journal.sqlite3"
    reports = [
        [wire.DeviceInfo(native_id="A", platform="android", name="A", capabilities=[])],
        [wire.DeviceInfo(native_id="A", platform="android", name="A", capabilities=[])],
        [
            wire.DeviceInfo(
                native_id="A",
                platform="android",
                name="A",
                capabilities=[],
                problems=["disconnected"],
            )
        ],
    ]

    async def scan() -> list[wire.DeviceInfo]:
        if not reports:
            raise asyncio.CancelledError
        return reports.pop(0)

    client = GatewayClient(
        cloud_url="http://127.0.0.1:1",
        credentials=stored(journal, "wgw_test"),
        journal=Journal(journal),
        devices=[],
        scan=scan,
    )
    client._reported = await client._inventory()  # what the hello said
    sent: list[str] = []

    class Socket:
        async def send(self, raw: str) -> None:
            sent.append(raw)

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    with pytest.raises(asyncio.CancelledError):
        await client._watch_devices(Socket())
    (message,) = [wire.Devices.model_validate_json(raw) for raw in sent]  # only the change
    assert message.devices[0].problems == ["disconnected"]


async def test_a_change_found_between_scans_is_reported_without_waiting_for_the_next_scan(
    tmp_path: Path,
) -> None:
    from witx_contracts import gateway as wire

    journal = tmp_path / "journal.sqlite3"
    changed = asyncio.Event()
    reports = [
        [
            wire.DeviceInfo(
                native_id="A",
                platform="android",
                name="A",
                capabilities=[],
                problems=["not_responding"],
            )
        ]
    ]

    async def scan() -> list[wire.DeviceInfo]:
        if not reports:
            raise asyncio.CancelledError
        changed.set()  # it changes again while this report goes out
        return reports.pop(0)

    client = GatewayClient(
        cloud_url="http://127.0.0.1:1",
        credentials=stored(journal, "wgw_test"),
        journal=Journal(journal),
        devices=[],
        scan=scan,
        rescan=changed,
    )
    client._reported = [
        wire.DeviceInfo(native_id="A", platform="android", name="A", capabilities=[])
    ]
    sent: list[str] = []

    class Socket:
        async def send(self, raw: str) -> None:
            sent.append(raw)

    changed.set()  # a phone stopped answering (discovery.Inventory.changed)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(client._watch_devices(Socket()), 2)  # well before SCAN_SECONDS
    (message,) = [wire.Devices.model_validate_json(raw) for raw in sent]
    assert message.devices[0].problems == ["not_responding"]


async def test_input_under_way_when_the_connection_drops_finishes_and_reports_later(
    tmp_path: Path,
) -> None:
    import json
    import socket
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from websockets.asyncio.server import serve
    from witx_contracts import gateway as wire
    from witx_gateway.devices.fake import FakeDevice

    device = FakeDevice(input_seconds=0.5)  # still typing when the cloud goes away
    device_id = uuid4()
    action = wire.Action(
        action_id=uuid4(),
        device_id=device_id,
        epoch=7,
        input={"kind": "tap", "x": 300, "y": 600},  # type: ignore[arg-type]
        expected=wire.ScreenState(
            width=device.width,
            height=device.height,
            input_width=device.width,
            input_height=device.height,
            rotation=0,
            foreground_app=device.foreground_app,
        ),
        digest="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        post_observation_id=uuid4(),
    )
    welcome = wire.Welcome(
        gateway_id=uuid4(),
        devices=[wire.DeviceBinding(native_id=device.native_id, device_id=device_id)],
    ).model_dump_json()
    connections = 0
    after_reconnect: list[dict] = []  # type: ignore[type-arg]
    done = asyncio.Event()

    async def cloud(ws) -> None:  # type: ignore[no-untyped-def]
        nonlocal connections
        connections += 1
        await ws.recv()  # hello
        await ws.send(welcome)
        if connections == 1:
            await ws.send(action.model_dump_json())
            await asyncio.sleep(0.1)
            return  # the connection drops while the input is under way
        async for raw in ws:
            message = json.loads(raw)
            if message["type"] != "heartbeat":
                after_reconnect.append(message)
            if {m["type"] for m in after_reconnect} >= {"action_result", "observation_result"}:
                done.set()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    journal = tmp_path / "journal.sqlite3"
    client = GatewayClient(
        cloud_url=f"http://127.0.0.1:{port}",
        credentials=stored(journal, "wgw_test"),
        journal=Journal(journal),
        devices=[device],
    )
    client.journal.set_epoch(device_id, 7)
    stop = asyncio.Event()
    async with serve(cloud, "127.0.0.1", port):
        running = asyncio.create_task(client.run(stop))
        await asyncio.wait_for(done.wait(), 15)
        stop.set()
        await running
    assert device.inputs == [("tap", (300.0, 600.0))]  # once
    results = {m["type"]: m for m in after_reconnect}
    assert results["action_result"]["outcome"] == "returned"
    assert results["observation_result"]["observation_id"] == str(action.post_observation_id)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_after_a_reconnect_only_the_results_the_cloud_did_not_acknowledge_are_sent_again(
    tmp_path: Path,
) -> None:
    import json
    from uuid import uuid4

    from websockets.asyncio.server import serve
    from witx_contracts import gateway as wire

    stored_id, lost_id = uuid4(), uuid4()
    welcome = wire.Welcome(gateway_id=uuid4(), devices=[]).model_dump_json()
    received: list[list[str]] = []  # the results each connection brought
    done = asyncio.Event()

    async def cloud(ws) -> None:  # type: ignore[no-untyped-def]
        await ws.recv()  # hello
        await ws.send(welcome)
        received.append([])
        async for raw in ws:
            message = json.loads(raw)
            if message["type"] == "heartbeat":
                continue
            received[-1].append(message["observation_id"])
            if len(received) == 1 and message["observation_id"] == str(stored_id):
                await ws.send(wire.ResultAck(ref_id=stored_id).model_dump_json())
            if len(received) == 1 and len(received[0]) == 2:
                await asyncio.sleep(0.1)  # the acknowledgement goes out first
                return  # the connection drops before the other one is acknowledged
            if len(received) == 2:
                done.set()

    journal = tmp_path / "journal.sqlite3"
    client = GatewayClient(
        cloud_url=f"http://127.0.0.1:{(port := free_port())}",
        credentials=stored(journal, "wgw_test"),
        journal=Journal(journal),
        devices=[],
    )
    for ref_id in (stored_id, lost_id):  # results waiting for the cloud
        result = wire.ObservationResult(observation_id=ref_id, error="locked")
        client.journal.enqueue(ref_id, result.model_dump_json())
    stop = asyncio.Event()
    async with serve(cloud, "127.0.0.1", port):
        running = asyncio.create_task(client.run(stop))
        await asyncio.wait_for(done.wait(), 15)
        stop.set()
        await running
    assert received == [[str(stored_id), str(lost_id)], [str(lost_id)]]


async def test_a_credential_that_cannot_be_stored_is_not_used_and_the_gateway_stays_signed_in(
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    import httpx
    from keyring.errors import KeyringError
    from websockets.asyncio.server import serve
    from witx_contracts import gateway as wire
    from witx_gateway.credentials import CredentialStore

    from tests.gateway.keystore import MemoryKeyring

    keyring = MemoryKeyring()
    # No key store will take the next credential, and its file cannot be written either.
    credentials = CredentialStore(tmp_path / "missing" / "journal.sqlite3", keyring)
    credentials.save("wgw_old")

    def refuse(service: str, username: str, password: str) -> None:
        raise KeyringError("locked")

    keyring.set_password = refuse  # type: ignore[method-assign]
    issued: list[str] = []

    def issue(request: httpx.Request) -> httpx.Response:
        issued.append(request.headers["Authorization"])
        return httpx.Response(200, json={"gateway_id": str(uuid4()), "credential": "wgw_new"})

    signed_in: list[str] = []

    async def cloud(ws) -> None:  # type: ignore[no-untyped-def]
        signed_in.append(ws.request.headers["Authorization"])
        await ws.recv()  # hello
        await ws.send(
            wire.Welcome(gateway_id=uuid4(), devices=[], rotate_credential=True).model_dump_json()
        )
        async for _ in ws:
            pass

    client = GatewayClient(
        cloud_url=f"http://127.0.0.1:{(port := free_port())}",
        credentials=credentials,
        journal=Journal(tmp_path / "journal.sqlite3"),
        devices=[],
        http=httpx.AsyncClient(transport=httpx.MockTransport(issue)),
    )
    stop = asyncio.Event()
    async with serve(cloud, "127.0.0.1", port):
        running = asyncio.create_task(client.run(stop))
        await asyncio.wait_for(client.connected.wait(), 5)
        stop.set()
        await running
    assert issued == ["Bearer wgw_old"]  # it asked for the next one
    assert (client.token, credentials.load()) == ("wgw_old", "wgw_old")
    assert signed_in == ["Bearer wgw_old"]  # and went on with this connection, not a new one


def test_a_cloud_on_another_machine_must_be_reached_over_https() -> None:
    assert checked_cloud_url("https://witx.example.com/") == "https://witx.example.com"
    assert checked_cloud_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="not encrypted"):
        checked_cloud_url("http://witx.example.com")
    # A trusted local network may say so explicitly.
    assert checked_cloud_url("http://192.168.1.20:8000", allow_insecure=True)
    with pytest.raises(ValueError):
        checked_cloud_url("ftp://witx.example.com")
