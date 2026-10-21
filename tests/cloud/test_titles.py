"""Task titles summarised from the first message, for the task list."""

import json
from dataclasses import dataclass, field

from witx.domain import rules
from witx.models import ModelError, ModelRequest, ModelResponse
from witx.services import lifecycle, titles
from witx.store import tasks
from witx.store.db import Database, execute, fetch_one

from tests.cloud.support import provision


@dataclass
class Titler:
    answer: str | None = "查 999 感冒灵价格"
    requests: list[ModelRequest] = field(default_factory=list)

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.answer is None:
            raise ModelError("http_500", "down", retryable=True)
        call = {
            "type": "function_call",
            "name": "set_title",
            "arguments": json.dumps({"title": self.answer}, ensure_ascii=False),
        }
        return ModelResponse(id="r", output=[call], usage={}, raw={})


async def new_task(db: Database, admin_db: Database, goal: str):  # type: ignore[no-untyped-def]
    tenant = await provision(admin_db, "acme")
    async with db.tenant(tenant.id) as conn:
        task = await lifecycle.create_task(
            conn,
            tenant_id=tenant.id,
            user_id=tenant.user_id,
            request=goal,
            constraints=[],
            device_ids=[],
            model="test-model",
        )
    return tenant, task


async def test_a_new_task_gets_a_title_from_its_first_message(
    db: Database, admin_db: Database
) -> None:
    goal = "上网查一下 999 感冒灵颗粒 10g 9袋 现在常见的零售价大概多少钱 告诉我来源"
    tenant, task = await new_task(db, admin_db, goal)
    model = Titler()
    assert await titles.title_tasks(db, model, "test-model") == 1
    async with db.tenant(tenant.id) as conn:
        titled = await tasks.get(conn, task.id)
    assert titled is not None and titled.title == "查 999 感冒灵价格"
    assert goal in str(model.requests[0].input)  # summarised from the first message
    assert await titles.title_tasks(db, model, "test-model") == 0  # not asked again


async def test_a_task_the_model_cannot_title_keeps_its_goal_after_a_few_tries(
    db: Database, admin_db: Database
) -> None:
    tenant, task = await new_task(db, admin_db, "Open Settings")
    model = Titler(answer=None)
    for _ in range(5):
        await titles.title_tasks(db, model, "test-model")
    assert len(model.requests) == tasks.TITLE_TRIES
    async with db.tenant(tenant.id) as conn:
        assert (await tasks.get(conn, task.id)).title is None  # type: ignore[union-attr]


def test_a_title_is_one_clean_line() -> None:
    assert rules.task_title(' "Check the battery." ') == "Check the battery"
    assert rules.task_title("\u201c\u67e5\u7535\u91cf\u201d\u3002") == "\u67e5\u7535\u91cf"
    assert rules.task_title("   ") is None


async def test_a_task_deleted_while_it_is_titled_stays_untitled(
    db: Database, admin_db: Database
) -> None:
    _, task = await new_task(db, admin_db, "Open Settings")
    model = Titler()
    respond = model.respond

    async def deleted_meanwhile(request: ModelRequest) -> ModelResponse:
        async with admin_db.system() as conn:
            await execute(conn, "UPDATE tasks SET deleted_at = now() WHERE id = :t", t=task.id)
        return await respond(request)

    model.respond = deleted_meanwhile  # type: ignore[method-assign]
    await titles.title_tasks(db, model, "test-model")
    async with admin_db.system() as conn:
        row = await fetch_one(conn, "SELECT title FROM tasks WHERE id = :t", t=task.id)
    assert row is not None and row["title"] is None
