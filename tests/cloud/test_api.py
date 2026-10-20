"""Write requests with an Idempotency-Key: a retry gets the first response instead of
acting again ."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator
from witx.config import Settings, parse_models
from witx.domain import rules
from witx.domain.model import Repeat
from witx.hub import Hub
from witx.objects import LocalObjectStore
from witx.services import identity as identity_service
from witx.services import pool as pool_service
from witx.services import schedules as schedule_service
from witx.services.identity import Actor
from witx.store import identity as identity_store
from witx.store import tasks as tasks_store
from witx.store.db import Database, execute

from tests.cloud.support import Tenant, provision
from tests.conftest import Databases

TOKEN = "dev-token-acme"


@pytest.fixture
async def client(
    databases: Databases, db: Database, admin_db: Database, tmp_path: Path, request: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, Tenant]]:
    """The API as the tenant's owner. A test marked ``models("...")`` gets that catalog."""
    tenant = await provision(admin_db, "acme", devices=1)
    marker = request.node.get_closest_marker("models")
    settings = Settings(
        database_url=databases.app_url,
        object_store_url=f"file://{tmp_path}",
        dev_tokens={TOKEN: tenant.subject},
        models=parse_models(marker.args[0]) if marker else {},
    )
    objects = LocalObjectStore(tmp_path)
    app = create_app(
        settings,
        db=db,
        objects=objects,
        authenticator=DevAuthenticator(settings.dev_tokens),
        hub=Hub(db, objects, settings.limits),
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c, tenant


def keyed(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


async def test_a_retried_task_creation_creates_one_task(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])]}
    first = await web.post("/api/v1/tasks", json=body, headers=keyed("k-create"))
    again = await web.post("/api/v1/tasks", json=body, headers=keyed("k-create"))
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert len((await web.get("/api/v1/tasks")).json()) == 1


async def test_concurrent_requests_with_the_same_key_create_one_task(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])]}
    responses = await asyncio.gather(
        *(web.post("/api/v1/tasks", json=body, headers=keyed("k-race")) for _ in range(5))
    )
    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    assert len((await web.get("/api/v1/tasks")).json()) == 1


async def test_the_same_key_with_another_request_is_a_conflict(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    device = str(tenant.device_ids[0])
    task = await web.post(
        "/api/v1/tasks", json={"goal": "A", "device_ids": [device]}, headers=keyed("k1")
    )
    other_content = await web.post(
        "/api/v1/tasks", json={"goal": "B", "device_ids": [device]}, headers=keyed("k1")
    )
    other_endpoint = await web.post(f"/api/v1/tasks/{task.json()['id']}/stop", headers=keyed("k1"))
    for response in (other_content, other_endpoint):
        assert response.status_code == 409
        assert response.json()["code"] == "idempotency_key_reused"
    assert len((await web.get("/api/v1/tasks")).json()) == 1


async def test_a_retried_input_is_added_once(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    task = (
        await web.post(
            "/api/v1/tasks", json={"goal": "A", "device_ids": [str(tenant.device_ids[0])]}
        )
    ).json()
    note = {"text": "also check Wi-Fi"}
    for _ in range(2):
        response = await web.post(
            f"/api/v1/tasks/{task['id']}/inputs", json=note, headers=keyed("k-note")
        )
        assert response.status_code == 200
    inputs = (await web.get(f"/api/v1/tasks/{task['id']}")).json()["inputs"]
    assert [i["kind"] for i in inputs] == ["goal", "note"]


async def test_a_failed_write_is_not_remembered(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    unknown = {"goal": "A", "device_ids": [str(uuid4())]}
    failed = await web.post("/api/v1/tasks", json=unknown, headers=keyed("k-retry"))
    assert failed.status_code == 404
    valid = {"goal": "A", "device_ids": [str(tenant.device_ids[0])]}
    retried = await web.post("/api/v1/tasks", json=valid, headers=keyed("k-retry"))
    assert retried.status_code == 201  # the key was not used up by the failure


def test_the_request_digest_ignores_key_order_and_spacing() -> None:
    assert rules.request_digest({"a": 1, "b": [1, 2]}) == rules.request_digest(
        {"b": [1, 2], "a": 1}
    )
    assert rules.request_digest({"a": 1}) != rules.request_digest({"a": 2})


async def test_the_model_catalog_is_listed_and_a_model_outside_it_is_refused(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    models = (await web.get("/api/v1/models")).json()
    assert models == [
        {
            "name": "gpt-6-luna",
            "provider": "openai",
            "default": True,
            "allowed": True,
            "efforts": ["medium", "high", "xhigh", "max"],
        }
    ]
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])], "model": "gpt-4"}
    refused = await web.post("/api/v1/tasks", json=body)
    assert refused.status_code == 422 and refused.json()["code"] == "unknown_model"
    created = await web.post("/api/v1/tasks", json={**body, "model": "gpt-6-luna"})
    switch = await web.put(f"/api/v1/tasks/{created.json()['id']}/model", json={"model": "gpt-4"})
    assert switch.status_code == 422


async def test_tasks_are_filed_in_groups_and_a_deleted_group_leaves_them_ungrouped(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])]}
    task = (await web.post("/api/v1/tasks", json=body)).json()["id"]
    group = (await web.post("/api/v1/task-groups", json={"name": "  Errands  "})).json()
    assert group["name"] == "Errands"

    moved = await web.put(f"/api/v1/tasks/{task}/group", json={"group_id": group["id"]})
    assert moved.json()["group_id"] == group["id"]
    in_group = (await web.get("/api/v1/tasks", params={"group": group["id"]})).json()
    assert [t["id"] for t in in_group] == [task]
    assert (await web.get("/api/v1/tasks", params={"group": "none"})).json() == []

    assert (await web.delete(f"/api/v1/task-groups/{group['id']}")).status_code == 204
    assert (await web.get("/api/v1/task-groups")).json() == []
    (left,) = (await web.get("/api/v1/tasks", params={"group": "none"})).json()
    assert left["id"] == task and left["group_id"] is None


async def test_a_task_cannot_be_filed_in_a_group_that_does_not_exist(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])]}
    task = (await web.post("/api/v1/tasks", json=body)).json()["id"]
    missing = await web.put(f"/api/v1/tasks/{task}/group", json={"group_id": str(uuid4())})
    assert missing.status_code == 404


async def test_a_task_can_be_filed_in_a_group_as_it_is_created(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    group = (await web.post("/api/v1/task-groups", json={"name": "Errands"})).json()
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])]}
    created = await web.post("/api/v1/tasks", json={**body, "group_id": group["id"]})
    assert created.status_code == 201
    in_group = (await web.get("/api/v1/tasks", params={"group": group["id"]})).json()
    assert [t["id"] for t in in_group] == [created.json()["id"]]

    missing = await web.post("/api/v1/tasks", json={**body, "group_id": str(uuid4())})
    assert missing.status_code == 404
    assert len((await web.get("/api/v1/tasks")).json()) == 1  # nothing was created


async def test_a_schedule_does_not_take_a_group(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    group = (await web.post("/api/v1/task-groups", json={"name": "Errands"})).json()
    body = {
        "goal": "Check the battery",
        "device_ids": [str(tenant.device_ids[0])],
        "repeat": "daily",
        "time_of_day": "08:30",
        "timezone": "Asia/Shanghai",
        "group_id": group["id"],
    }
    assert (await web.post("/api/v1/schedules", json=body)).status_code == 422


async def test_the_task_list_can_show_only_tasks_that_wait_for_someone(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])]}
    await web.post("/api/v1/tasks", json=body)
    assert (await web.get("/api/v1/tasks", params={"attention": "true"})).json() == []
    assert len((await web.get("/api/v1/tasks", params={"limit": 1})).json()) == 1


def test_a_group_needs_a_short_name() -> None:
    assert rules.group_name("  Weekly   shopping ") == "Weekly shopping"
    with pytest.raises(rules.Rejected):
        rules.group_name("   ")
    with pytest.raises(rules.Rejected):
        rules.group_name("x" * 81)


async def test_tasks_are_found_by_words_in_what_was_asked(client) -> None:  # type: ignore[no-untyped-def]
    web, _ = client
    for goal in ("Open Settings", "Buy 100% juice", "Send a message"):
        await web.post("/api/v1/tasks", json={"goal": goal, "device_ids": []})
    found = (await web.get("/api/v1/tasks", params={"q": "settings"})).json()
    assert [t["request"] for t in found] == ["Open Settings"]
    literal = (await web.get("/api/v1/tasks", params={"q": "100%"})).json()
    assert [t["request"] for t in literal] == ["Buy 100% juice"]


async def test_an_owner_adds_a_device_pool_and_gets_its_code_once(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    created = await web.post("/api/v1/gateways", json={"name": " Office  Mac "})
    assert created.status_code == 201
    body = created.json()
    assert body["gateway"]["name"] == "Office Mac" and body["gateway"]["state"] == "waiting"
    assert body["code"].startswith(f"{tenant.id}.")
    listed = (await web.get("/api/v1/gateways")).json()
    assert {g["name"]: g["state"] for g in listed} == {
        "acme-gw": "enrolled",
        "Office Mac": "waiting",
    }
    assert "code" not in str(listed)  # never shown again


async def test_a_device_pool_can_be_disabled_enabled_and_revoked_for_good(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    pool = f"/api/v1/gateways/{tenant.gateway_id}"
    assert (await web.post(f"{pool}/disable")).json()["state"] == "disabled"
    assert (await web.post(f"{pool}/enable")).json()["state"] == "enrolled"
    assert (await web.post(f"{pool}/revoke")).json()["state"] == "revoked"
    assert (await web.post(f"{pool}/enable")).status_code == 404  # revoking is for good


async def test_only_an_owner_manages_device_pools(db: Database, admin_db: Database) -> None:
    tenant = await provision(admin_db, "acme")
    member = Actor(tenant.user_id, "Acme", tenant.id, "acme", "member")
    async with db.tenant(tenant.id) as conn:
        with pytest.raises(rules.Rejected, match="owner_required"):
            await pool_service.add_gateway(conn, member, "Office", datetime.now(UTC))


async def test_a_schedule_is_made_listed_paused_and_deleted(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    body = {
        "goal": "Check the battery",
        "device_ids": [str(tenant.device_ids[0])],
        "repeat": "weekdays",
        "time_of_day": "08:30",
        "timezone": "Asia/Shanghai",
    }
    created = await web.post("/api/v1/schedules", json=body)
    assert created.status_code == 201
    schedule = created.json()
    assert schedule["enabled"] and schedule["next_run_at"] is not None
    assert [s["id"] for s in (await web.get("/api/v1/schedules")).json()] == [schedule["id"]]

    paused = await web.patch(f"/api/v1/schedules/{schedule['id']}", json={"enabled": False})
    assert paused.json()["enabled"] is False and paused.json()["next_run_at"] is None
    assert (await web.delete(f"/api/v1/schedules/{schedule['id']}")).status_code == 204
    assert (await web.get("/api/v1/schedules")).json() == []


async def test_a_schedule_that_cannot_run_is_refused(client) -> None:  # type: ignore[no-untyped-def]
    web, _ = client
    base = {"goal": "Check", "time_of_day": "08:00", "timezone": "Asia/Shanghai"}
    past = {**base, "repeat": "once", "run_on": "2020-01-01"}
    assert (await web.post("/api/v1/schedules", json=past)).status_code == 422
    no_days = {**base, "repeat": "weekly", "weekdays": []}
    assert (await web.post("/api/v1/schedules", json=no_days)).status_code == 422


async def test_an_owner_turns_on_the_fast_risk_check_and_a_member_cannot(
    client, db: Database, admin_db: Database
) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    assert (await web.get("/api/v1/me")).json()["fast_risk_check"] is False
    changed = await web.put("/api/v1/tenant/settings", json={"fast_risk_check": True})
    assert changed.json() == {
        "allow_full_access": False,
        "fast_risk_check": True,
        "allowed_providers": ["openai"],
        "model_fallback": False,
    }
    assert (await web.get("/api/v1/me")).json()["fast_risk_check"] is True

    member = Actor(tenant.user_id, "Acme", tenant.id, "acme", "member")
    async with db.system() as conn:
        with pytest.raises(rules.Rejected, match="owner_required"):
            await identity_service.set_fast_risk_check(conn, member, False)


TWO_PROVIDERS = "gpt-6-luna=openai,flash=deepinfra:deepseek-ai/DeepSeek-V4.1-Flash"


@pytest.mark.models(TWO_PROVIDERS)
async def test_until_an_owner_allows_another_provider_its_models_cannot_be_chosen(client) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    models = {m["name"]: m["allowed"] for m in (await web.get("/api/v1/models")).json()}
    assert models == {"gpt-6-luna": True, "flash": False}
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])], "model": "flash"}
    refused = await web.post("/api/v1/tasks", json=body)
    assert refused.status_code == 403 and refused.json()["code"] == "provider_not_allowed"
    task = (await web.post("/api/v1/tasks", json={**body, "model": "gpt-6-luna"})).json()
    switch = await web.put(f"/api/v1/tasks/{task['id']}/model", json={"model": "flash"})
    assert switch.status_code == 403

    allowed = await web.put("/api/v1/tenant/settings", json={"allowed_providers": ["deepinfra"]})
    assert allowed.json()["allowed_providers"] == ["deepinfra"]
    assert (await web.get("/api/v1/me")).json()["allowed_providers"] == ["deepinfra"]
    switched = await web.put(f"/api/v1/tasks/{task['id']}/model", json={"model": "flash"})
    assert switched.status_code == 200
    refused = await web.post("/api/v1/tasks", json={**body, "model": "gpt-6-luna"})
    assert refused.json()["code"] == "provider_not_allowed"  # OpenAI is no longer allowed


@pytest.mark.models(TWO_PROVIDERS)
async def test_an_owner_allows_known_providers_only_and_a_member_cannot_choose(
    client, db: Database
) -> None:  # type: ignore[no-untyped-def]
    web, tenant = client
    for chosen in ([], ["anthropic"]):
        refused = await web.put("/api/v1/tenant/settings", json={"allowed_providers": chosen})
        assert refused.status_code == 422 and refused.json()["code"] == "invalid_providers"
    member = Actor(tenant.user_id, "Acme", tenant.id, "acme", "member")
    async with db.system() as conn:
        with pytest.raises(rules.Rejected, match="owner_required"):
            await identity_service.set_allowed_providers(conn, member, ["openai"], {"openai"})


async def test_a_task_thinks_as_hard_as_chosen_and_never_below_medium(client) -> None:  # type: ignore[no-untyped-def]
    web, _ = client
    task = (
        await web.post("/api/v1/tasks", json={"goal": "Compare prices", "reasoning_effort": "high"})
    ).json()
    assert task["reasoning_effort"] == "high"
    changed = await web.put(
        f"/api/v1/tasks/{task['id']}/reasoning-effort", json={"reasoning_effort": "max"}
    )
    assert changed.json()["reasoning_effort"] == "max"
    low = await web.put(
        f"/api/v1/tasks/{task['id']}/reasoning-effort", json={"reasoning_effort": "low"}
    )
    assert low.status_code == 422
    plain = (await web.post("/api/v1/tasks", json={"goal": "Open Settings"})).json()
    assert plain["reasoning_effort"] == "medium"


@pytest.mark.models("gpt-6-luna=openai,gpt-6.1-sol=openai@low")
async def test_a_model_that_goes_lower_thinks_at_low_and_a_switch_raises_it_to_the_next(  # type: ignore[no-untyped-def]
    client,
) -> None:
    web, _ = client
    models = (await web.get("/api/v1/models")).json()
    assert {m["name"]: m["efforts"][0] for m in models} == {
        "gpt-6-luna": "medium",
        "gpt-6.1-sol": "low",
    }
    goal = {"goal": "Open Settings", "reasoning_effort": "low"}
    refused = await web.post("/api/v1/tasks", json=goal | {"model": "gpt-6-luna"})
    assert refused.status_code == 422 and refused.json()["code"] == "invalid_effort"
    task = (await web.post("/api/v1/tasks", json=goal | {"model": "gpt-6.1-sol"})).json()
    assert task["reasoning_effort"] == "low"
    moved = await web.put(f"/api/v1/tasks/{task['id']}/model", json={"model": "gpt-6-luna"})
    assert (moved.json()["model"], moved.json()["reasoning_effort"]) == ("gpt-6-luna", "medium")


async def test_a_proposed_schedule_is_in_the_time_zone_the_task_was_started_in(  # type: ignore[no-untyped-def]
    client, db: Database
) -> None:
    web, tenant = client
    task = (
        await web.post(
            "/api/v1/tasks", json={"goal": "Every day at 8", "timezone": "Asia/Shanghai"}
        )
    ).json()
    async with db.tenant(tenant.id) as conn:
        proposal = await schedule_service.propose(
            conn,
            await tasks_store.get(conn, UUID(task["id"])),  # type: ignore[arg-type]
            goal="Check the battery",
            repeat=Repeat.DAILY,
            time_of_day=time(8, 0),
            weekdays=[],
            month_day=None,
            run_on=None,
            now=datetime.now(UTC),
        )
    assert proposal.timezone == "Asia/Shanghai"
    turned_on = await web.patch(f"/api/v1/schedules/{proposal.id}", json={"enabled": True})
    body = turned_on.json()
    assert body["enabled"] is True and body["timezone"] == "Asia/Shanghai"
    local = datetime.fromisoformat(body["next_run_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local.hour, local.minute) == (8, 0)


async def test_a_task_with_an_unknown_time_zone_is_refused(client) -> None:  # type: ignore[no-untyped-def]
    web, _ = client
    refused = await web.post("/api/v1/tasks", json={"goal": "Check", "timezone": "Mars/Base"})
    assert refused.status_code == 422 and refused.json()["code"] == "unknown_timezone"


async def test_a_phone_is_taken_over_from_the_pool_and_shown_as_such(  # type: ignore[no-untyped-def]
    client, db: Database
) -> None:
    web, tenant = client
    device_id = tenant.device_ids[0]
    async with db.tenant(tenant.id) as conn:
        await execute(conn, "UPDATE devices SET online = true WHERE id = :d", d=device_id)
    started = await web.post(f"/api/v1/devices/{device_id}/takeover", headers=keyed("t1"))
    assert started.status_code == 201
    (shown,) = (await web.get("/api/v1/devices")).json()
    assert shown["taken_over"] is True and shown["task_id"] is None
    (change, *_) = (await web.get(f"/api/v1/devices/{device_id}/changes")).json()
    assert change["change"] == "taken_over"

    takeover_id = started.json()["takeover_id"]
    ended = await web.post(f"/api/v1/takeovers/{takeover_id}/end", json={})
    assert ended.status_code == 200 and ended.json() is None
    (shown,) = (await web.get("/api/v1/devices")).json()
    assert shown["taken_over"] is False


async def test_a_side_chat_question_waits_for_its_answer_and_closing_the_tab_forgets_it(  # type: ignore[no-untyped-def]
    client,
) -> None:
    web, _ = client
    task = (await web.post("/api/v1/tasks", json={"goal": "Check the battery"})).json()
    thread = str(uuid4())
    asked = await web.post(
        f"/api/v1/tasks/{task['id']}/side-chat", json={"thread_id": thread, "text": "Why?"}
    )
    assert asked.status_code == 201
    assert asked.json()["answer"] is None  # the worker answers it
    (listed,) = (await web.get(f"/api/v1/tasks/{task['id']}/side-chat")).json()
    assert listed["question"] == "Why?" and listed["thread_id"] == thread

    closed = await web.delete(f"/api/v1/tasks/{task['id']}/side-chat/{thread}")
    assert closed.status_code == 204
    assert (await web.get(f"/api/v1/tasks/{task['id']}/side-chat")).json() == []


def test_a_wire_model_writes_every_moment_in_utc_however_it_was_held() -> None:
    from datetime import datetime, timedelta, timezone

    from witx_contracts.api import TaskResult

    held = datetime(2026, 10, 18, 19, 29, 57, tzinfo=timezone(timedelta(hours=8)))
    result = TaskResult(summary="done", evidence_observation_ids=[], completed_at=held)
    assert result.model_dump(mode="json")["completed_at"] == "2026-10-18T11:29:57Z"


async def test_pinned_tasks_are_listed_apart_latest_pinned_first_and_only_for_who_pinned_them(  # type: ignore[no-untyped-def]
    client, admin_db: Database
) -> None:
    web, tenant = client
    ids = [
        (await web.post("/api/v1/tasks", json={"goal": goal, "device_ids": []})).json()["id"]
        for goal in ("First", "Second", "Third")
    ]
    for task in (ids[0], ids[2]):
        assert (await web.put(f"/api/v1/tasks/{task}/pin")).status_code == 204
    assert (await web.put(f"/api/v1/tasks/{ids[0]}/pin")).status_code == 204  # again: fine

    only = (await web.get("/api/v1/tasks", params={"pinned": "only"})).json()
    assert [t["id"] for t in only] == [ids[2], ids[0]] and all(t["pinned"] for t in only)
    without = (await web.get("/api/v1/tasks", params={"pinned": "without"})).json()
    assert [t["id"] for t in without] == [ids[1]] and not without[0]["pinned"]

    async with admin_db.system() as conn:  # someone else in the workspace
        other = await identity_store.ensure_user(conn, "user-bob", "Bob")
        await identity_store.ensure_membership(conn, tenant.id, other)
        assert await tasks_store.pinned(conn, other, [UUID(i) for i in ids]) == set()

    assert (await web.delete(f"/api/v1/tasks/{ids[0]}/pin")).status_code == 204
    only = (await web.get("/api/v1/tasks", params={"pinned": "only"})).json()
    assert [t["id"] for t in only] == [ids[2]]
    missing = await web.put(f"/api/v1/tasks/{uuid4()}/pin")
    assert missing.status_code == 404
