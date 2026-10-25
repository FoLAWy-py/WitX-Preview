"""WitX as an MCP server: AI clients give tasks to the phones; only a person
approves, in a form the client shows them, and payments only on the Web app."""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import ElicitRequestParams, ElicitResult, Implementation, InputRequiredResult
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator
from witx.config import Settings
from witx.domain import rules
from witx.domain.model import ActionStatus, PermissionMode, RiskVerdict
from witx.domain.rules import Rejected
from witx.services import retention
from witx.services.identity import Actor
from witx.store.db import Database, fetch_one

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import tap
from tests.conftest import Databases

TOKEN = "dev-token-acme"
Elicit = Callable[[Any, ElicitRequestParams], Awaitable[ElicitResult]]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


@pytest.fixture
async def server(rig: Rig, databases: Databases) -> AsyncIterator[str]:
    settings = Settings(
        database_url=databases.app_url,
        object_store_url="file:///unused",
        dev_tokens={TOKEN: rig.tenant.subject},
        public_url="http://web.example",
    )
    app = create_app(
        settings,
        db=rig.db,
        objects=rig.objects,
        authenticator=DevAuthenticator(settings.dev_tokens),
        hub=rig.hub,
    )
    port = free_port()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(uv.serve())
    while not uv.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/api/v1/mcp"
    finally:
        uv.should_exit = True
        with contextlib.suppress(Exception):
            await serving


@contextlib.asynccontextmanager
async def client(
    url: str, elicit: Elicit | None = None, protocol: str = "handshake"
) -> AsyncIterator[ClientSession]:
    """``handshake`` negotiates a 2025 version (questions arrive during the call);
    ``modern`` speaks 2026-07-28 (an input-required result, answered on a retry)."""
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with http, streamable_http_client(url, http_client=http) as (read, write, *_):
        session = ClientSession(
            read,
            write,
            elicitation_callback=elicit,
            client_info=Implementation(name="test-client", version="1"),
        )
        async with session:
            if protocol == "modern":
                await session.discover()
            else:
                await session.initialize()
            yield session


async def tool(
    session: ClientSession, name: str, elicit: Elicit | None = None, **arguments: Any
) -> Any:
    """Calls a tool as a client would on 2026-07-28 too: an input-required result is
    answered (the form shown to the person) and the call retried."""
    result = await session.call_tool(name, arguments, allow_input_required=True)
    while isinstance(result, InputRequiredResult):
        assert elicit is not None, "the server asked for input this client cannot give"
        responses = {
            key: await elicit(None, request.params)  # type: ignore[arg-type]
            for key, request in (result.input_requests or {}).items()
        }
        result = await session.call_tool(
            name,
            arguments,
            input_responses=responses,  # type: ignore[arg-type]
            request_state=result.request_state,
            allow_input_required=True,
        )
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        return content["result"] if set(content) == {"result"} else content  # lists are wrapped
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def held_action(rig: Rig, effects: list[str]) -> str:
    """A task whose tap waits for approval."""
    device = rig.tenant.device_ids[0]
    rig.risk.verdict = RiskVerdict(sensitive=True, effects=effects, reason="sends a message")
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, effects=effects),
    ]
    await rig.create_task("Send the note", permission_mode=PermissionMode.ASK)
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        row = await fetch_one(conn, "SELECT id FROM actions WHERE status = 'awaiting_approval'")
    assert row is not None
    return str(row["id"])


async def test_a_client_without_a_token_is_refused(server: str) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.post(server, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401


async def test_a_client_gives_the_phones_a_task_and_follows_it(rig: Rig, server: str) -> None:
    async with client(server) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
        assert {"create_task", "get_task", "review_approval", "deny_action"} <= tools
        assert "approve_action" not in tools  # no plain tool can approve
        devices = await tool(session, "list_devices")
        assert [d["id"] for d in devices] == [str(rig.tenant.device_ids[0])]
        created = await tool(
            session, "create_task", goal="Open Settings", device_ids=[devices[0]["id"]]
        )
        assert created["status"] == "active" and created["web_url"].startswith(
            "http://web.example/tasks/"
        )
        followed = await tool(session, "get_task", task_id=created["task_id"])
        assert followed["request"] == "Open Settings" and followed["goal"] == ""
        full = await session.call_tool(
            "create_task",
            {"goal": "x", "device_ids": [devices[0]["id"]], "permission_mode": "full"},
        )
        assert full.is_error  # full access is turned on in the Web app only


@pytest.mark.parametrize("protocol", ["handshake", "modern"])
async def test_a_person_approves_in_the_form_the_client_shows_them(
    rig: Rig, server: str, protocol: str
) -> None:
    action_id = await held_action(rig, ["send_message"])
    shown: list[str] = []

    async def person(context: Any, params: ElicitRequestParams) -> ElicitResult:
        shown.append(params.message)
        return ElicitResult(action="accept", content={"decision": "approve"})

    async with client(server, person, protocol) as session:
        answer = await tool(session, "review_approval", person, action_id=action_id)
    assert answer["status"] == "queued" and answer["decided_by"] == "user", answer
    assert "Wi-Fi row" in shown[0] and "http://web.example/tasks/" in shown[0]
    async with rig.db.tenant(rig.tenant.id) as conn:
        row = await fetch_one(
            conn,
            "SELECT decision, decided_via, decided_client FROM approvals WHERE action_id = :a",
            a=action_id,
        )
    assert row is not None and dict(row) == {
        "decision": "approve",
        "decided_via": "mcp",
        "decided_client": "test-client",
    }


@pytest.mark.parametrize("protocol", ["handshake", "modern"])
async def test_payments_and_clients_without_the_form_are_sent_to_the_web_app(
    rig: Rig, server: str, protocol: str
) -> None:
    payment = await held_action(rig, ["payment"])
    asked: list[str] = []

    async def person(context: Any, params: ElicitRequestParams) -> ElicitResult:
        asked.append(params.message)
        return ElicitResult(action="accept", content={"decision": "approve"})

    async with client(server, person, protocol) as session:
        answer = await tool(session, "review_approval", person, action_id=payment)
    assert answer["status"] == "awaiting_approval" and "approve_on_web" in answer
    assert asked == []  # the form was never shown for a payment
    async with client(server, protocol=protocol) as session:  # no elicitation support
        answer = await tool(session, "review_approval", action_id=payment)
    assert answer["approve_on_web"].startswith("http://web.example/tasks/")
    async with rig.db.tenant(rig.tenant.id) as conn:
        row = await fetch_one(conn, "SELECT status FROM actions WHERE id = :a", a=payment)
    assert row is not None and row["status"] == ActionStatus.AWAITING_APPROVAL


async def test_a_client_may_deny_without_asking_anyone(rig: Rig, server: str) -> None:
    action_id = await held_action(rig, ["send_message"])
    async with client(server) as session:
        denied = await tool(session, "deny_action", action_id=action_id)
    assert denied["status"] == "denied"


def test_only_the_web_app_approves_payments() -> None:
    rules.check_approval_channel(payment=False, via="mcp")
    rules.check_approval_channel(payment=True, via="web")
    with pytest.raises(Rejected, match="web_approval_required"):
        rules.check_approval_channel(payment=True, via="mcp")
    failed = RiskVerdict(sensitive=True, effects=[], reason="timeout", failed=True)
    assert rules.is_payment([], failed)  # a failed check cannot rule a payment out
    assert not rules.is_payment(["send_message"], None)


@pytest.mark.parametrize("protocol", ["handshake", "modern"])
@pytest.mark.parametrize("outcome", ["decline", "cancel"])
async def test_a_declined_or_closed_form_decides_nothing(
    rig: Rig, server: str, protocol: str, outcome: str
) -> None:
    action_id = await held_action(rig, ["send_message"])

    async def client_that_declines(context: Any, params: ElicitRequestParams) -> ElicitResult:
        return ElicitResult(action=outcome)  # type: ignore[arg-type]

    async with client(server, client_that_declines, protocol) as session:
        answer = await tool(session, "review_approval", client_that_declines, action_id=action_id)
    assert answer["status"] == "awaiting_approval" and "decided_by" not in answer
    assert answer["approve_on_web"].startswith("http://web.example/tasks/")
    async with rig.db.tenant(rig.tenant.id) as conn:
        row = await fetch_one(conn, "SELECT status FROM actions WHERE id = :a", a=action_id)
        approval = await fetch_one(
            conn, "SELECT decision FROM approvals WHERE action_id = :a", a=action_id
        )
    assert row is not None and row["status"] == ActionStatus.AWAITING_APPROVAL
    assert approval is None or approval["decision"] is None


async def waiting(rig: Rig) -> str:
    """A task that took a screenshot and asks the user something."""
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        [call("ask_user", question="Which network?")],
    ]
    task = await rig.create_task("Join the Wi-Fi")
    await rig.settle()
    return str(task.id)


async def test_a_client_messages_a_task_and_pauses_and_resumes_it_once_its_phone_confirms(
    rig: Rig, server: str
) -> None:
    task_id = await waiting(rig)
    async with client(server) as session:
        await tool(session, "add_input", task_id=task_id, text="The office one")
        async with rig.db.tenant(rig.tenant.id) as conn:
            row = await fetch_one(conn, "SELECT text FROM task_inputs WHERE kind = 'note'")
        assert row is not None and row["text"] == "The office one"

        paused = await tool(session, "control_task", task_id=task_id, action="pause")
        assert paused["status"] == "pausing"  # the phone has not confirmed yet
        await rig.gateway_round()
        assert (await tool(session, "get_task", task_id=task_id))["status"] == "paused"
        resumed = await tool(session, "control_task", task_id=task_id, action="resume")
        assert resumed["status"] == "active"


async def test_a_client_gets_a_tasks_screenshot_until_the_task_is_deleted(
    rig: Rig, server: str
) -> None:
    task_id = await waiting(rig)
    async with client(server) as session:
        shot = (await tool(session, "get_task", task_id=task_id))["latest_screenshots"][-1]
        image = await session.call_tool("get_screenshot", {"screenshot_id": shot})
        assert not image.is_error and image.content[0].type == "image"

        stopped = await tool(session, "control_task", task_id=task_id, action="stop")
        assert stopped["status"] == "stopping"  # until the phone confirms
        await rig.gateway_round()
        owner = Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "owner")
        async with rig.db.tenant(rig.tenant.id) as conn:
            await retention.delete_task(conn, owner, UUID(task_id), datetime.now(UTC), rig.objects)
        gone = await session.call_tool("get_screenshot", {"screenshot_id": shot})
        assert gone.is_error
