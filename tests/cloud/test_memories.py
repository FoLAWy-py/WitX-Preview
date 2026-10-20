"""Long-term memory : the agent keeps what it learns on its own,
only for the person who started the task, never secrets; notes about an app come when
the agent first sees that app; the person can turn it off."""

import json
from pathlib import Path

import pytest
from witx.api import views
from witx.domain import rules
from witx.domain.model import PermissionMode
from witx.harness.context import APP_NOTES_PREFIX
from witx.models import ModelRequest
from witx.services import lifecycle
from witx.services import memories as memory_service
from witx.store import identity, memories, tasks
from witx.store.db import Database

from tests.cloud.rig import SCREEN, Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete

APP = SCREEN["foreground_app"]


def test_a_memory_is_short_and_never_a_secret() -> None:
    assert rules.check_memory("  I order food   on Meituan ") == "I order food on Meituan"
    for secret in (
        "我的支付密码是 123456",
        "verification code 4471",
        "card 4111 1111 1111 1111",
        "CVV 123",
        "银行卡PIN码是4821",
        "安全码 123",
        "门禁码 2580",
        "api key sk-123",
    ):
        with pytest.raises(rules.Rejected, match="memory_not_allowed"):
            rules.check_memory(secret)
    with pytest.raises(rules.Rejected, match="invalid_memory"):
        rules.check_memory("x" * 301)
    with pytest.raises(rules.Rejected, match="memory_full"):
        rules.check_memory("one more", kept=rules.MEMORY_KEPT_MAX)
    with pytest.raises(rules.Rejected, match="invalid_memory"):
        rules.check_memory_kind("app", "not a package!")


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def remembered(rig: Rig, **arguments: object) -> list[dict[str, object]]:
    """Runs a task whose agent calls remember once; returns the tool results."""
    entry = {"kind": "user", "app": None, "replaces": None} | arguments
    rig.model.script = [[call("remember", **entry)], complete]
    task = await rig.create_task("Remember this")
    await rig.settle()
    return [o for o in await rig.outputs(task.id) if "memory_id" in o or "code" in o]


async def test_the_agent_keeps_what_it_learns_on_its_own_and_it_shows_in_the_task(
    rig: Rig,
) -> None:
    (saved,) = await remembered(rig, kind="feedback", text="Sign messages as Alex")
    assert saved["status"] == "saved"
    async with rig.db.tenant(rig.tenant.id) as conn:
        (entry,) = await memories.of_user(conn, rig.tenant.user_id)
        assert entry.source_task_id is not None
        task = await tasks.get(conn, entry.source_task_id)
        assert task is not None
        detail = await views.task_detail(conn, task)
    assert (entry.kind, entry.text, entry.by_agent) == ("feedback", "Sign messages as Alex", True)
    assert [m.text for m in detail.memories] == ["Sign messages as Alex"]


async def test_preferences_and_corrections_are_in_the_next_tasks_first_request(rig: Rig) -> None:
    await remembered(rig, text="I order food on Meituan")
    rig.model.requests.clear()
    rig.model.script = [complete]
    await rig.create_task("Order lunch")
    await rig.settle()
    first = json.dumps(rig.model.requests[0].input, ensure_ascii=False)
    assert "What is kept for this user" in first and "I order food on Meituan" in first


async def test_an_app_note_comes_once_when_a_screenshot_first_shows_that_app(rig: Rig) -> None:
    await remembered(rig, kind="app", app=APP, text="Wi-Fi is the first row")
    device = rig.tenant.device_ids[0]
    rig.model.requests.clear()
    rig.model.script = [[call("observe", device_id=device)], complete]
    await rig.create_task("Open Wi-Fi settings")
    await rig.settle()

    def notes(request: ModelRequest) -> int:
        return json.dumps(request.input, ensure_ascii=False).count(f"{APP_NOTES_PREFIX}{APP}")

    # In the first request (the first screenshot shows the app), and not added again.
    assert [notes(r) for r in rig.model.requests] == [1, 1]
    assert "Wi-Fi is the first row" in json.dumps(rig.model.requests[0].input)


async def test_with_auto_memory_off_the_agent_neither_keeps_nor_reads_its_own_entries(
    rig: Rig,
) -> None:
    await remembered(rig, text="I order food on Meituan")
    async with rig.db.system() as conn:
        await identity.set_auto_memory(conn, rig.tenant.id, rig.tenant.user_id, False)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await memory_service.add(
            conn, tenant_id=rig.tenant.id, user_id=rig.tenant.user_id, text="I live in Shanghai"
        )
    rig.model.requests.clear()
    (refused,) = await remembered(rig, text="Something new")
    first = json.dumps(rig.model.requests[0].input, ensure_ascii=False)
    assert refused["code"] == "memory_off"
    assert "I live in Shanghai" in first and "Meituan" not in first


async def test_someone_else_neither_gets_nor_deletes_a_persons_memory(
    rig: Rig, admin_db: Database
) -> None:
    await remembered(rig, text="I order food on Meituan")
    async with admin_db.system() as conn:
        other = await identity.ensure_user(conn, "user-bob", "Bob")
        await identity.ensure_membership(conn, rig.tenant.id, other)
    async with rig.db.tenant(rig.tenant.id) as conn:
        (entry,) = await memories.of_user(conn, rig.tenant.user_id)
        with pytest.raises(rules.Rejected, match="not_found"):
            await memory_service.delete(conn, memory_id=entry.id, user_id=other)
        bobs = await lifecycle.create_task(
            conn,
            tenant_id=rig.tenant.id,
            user_id=other,
            request="Order lunch",
            constraints=[],
            device_ids=[],
            model="test-model",
            permission_mode=PermissionMode.AUTO,
            timezone="UTC",
        )
        assert await memories.for_task(conn, bobs.id, auto=True) == []  # not Alice's


async def test_what_the_agent_may_keep_is_limited_to_what_it_can_know_for_that_person(
    rig: Rig, admin_db: Database
) -> None:
    # Only an app this task's screenshots showed.
    (refused,) = await remembered(rig, kind="app", app="com.other.app", text="Tap the tab")
    assert refused["code"] == "not_allowed"
    # Not the person's own words: an agent does not overwrite them.
    async with rig.db.tenant(rig.tenant.id) as conn:
        own = await memory_service.add(
            conn, tenant_id=rig.tenant.id, user_id=rig.tenant.user_id, text="I live in Shanghai"
        )
    (refused,) = await remembered(rig, text="I live in Beijing", replaces=str(own.id))
    assert refused["code"] == "not_found"
    # No preferences from a task someone else wrote in.
    async with admin_db.system() as conn:
        bob = await identity.ensure_user(conn, "user-bob", "Bob")
        await identity.ensure_membership(conn, rig.tenant.id, bob)
    rig.model.script = [
        [call("ask_user", question="Anything else?", to="user")],
        [call("remember", kind="user", app=None, text="Pay with Bob's card", replaces=None)],
        complete,
    ]
    task = await rig.create_task("Order lunch")
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(conn, task_id=task.id, user_id=bob, text="Use my card")
    await rig.settle()
    (refused,) = [o for o in await rig.outputs(task.id) if "code" in o]
    assert refused["code"] == "not_allowed"


async def test_a_sub_agent_keeps_only_notes_about_an_app(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("delegate", subtasks=[{"goal": "Look", "name": "A", "device_id": str(device)}])],
        [call("remember", kind="user", app=None, text="Prefers dark mode", replaces=None)],
        complete,
        complete,
    ]
    task = await rig.create_task("Check the settings")
    await rig.settle()
    refused = [o for o in await rig.outputs(task.id) if o.get("code") == "not_allowed"]
    assert len(refused) == 1
