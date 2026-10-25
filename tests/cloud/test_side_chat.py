"""Side chats: questions about a task, answered without acting."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from witx.api import views
from witx.models import ModelError, ModelRequest, ModelResponse
from witx.services import lifecycle, side_chat
from witx.store import side_chats, tasks
from witx.store.db import Database, fetch

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision, say


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def looked_and_said(rig: Rig):  # type: ignore[no-untyped-def]
    """A task whose agent took a screenshot and said what it saw, then waits."""
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        [say("The battery is at 80%."), call("ask_user", question="Anything else?")],
    ]
    task = await rig.create_task("Check the battery")
    await rig.settle()
    return task


async def ask(rig: Rig, task_id, thread_id, text: str):  # type: ignore[no-untyped-def]
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await side_chat.ask(
            conn, task_id=task_id, thread_id=thread_id, user_id=rig.tenant.user_id, question=text
        )


async def counts(rig: Rig, task_id) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    async with rig.db.tenant(rig.tenant.id) as conn:
        inputs = len(await tasks.inputs(conn, task_id))
        events = len(await tasks.events_after(conn, task_id, 0))
    return inputs, events


async def test_a_question_is_answered_from_the_task_without_acting_or_entering_it(
    rig: Rig,
) -> None:
    task = await looked_and_said(rig)
    before = await counts(rig, task.id)
    thread = uuid4()
    await ask(rig, task.id, thread, "What did it find?")
    rig.model.script = [[say("It found the battery at 80%.")]]

    assert await side_chat.answer_open(rig.db, rig.model, rig.objects) == 1  # type: ignore[arg-type]
    request = rig.model.requests[-1]
    assert request.tools == []  # it cannot act
    assert request.model == task.model
    sent = str(request.input)
    assert "The battery is at 80%." in sent and "input_image" in sent
    async with rig.db.tenant(rig.tenant.id) as conn:
        (message,) = await side_chats.for_task(conn, task.id)
    assert message.answer == "It found the battery at 80%."
    assert await counts(rig, task.id) == before  # the task's conversation is untouched

    # The next question in the tab sees this one; another tab does not.
    await ask(rig, task.id, thread, "And before that?")
    await ask(rig, task.id, uuid4(), "Which phone?")
    rig.model.script = [[say("Nothing before.")], [say("Pixel 0.")]]
    await side_chat.answer_open(rig.db, rig.model, rig.objects)  # type: ignore[arg-type]
    same, other = (str(r.input) for r in rig.model.requests[-2:])
    assert "What did it find?" in same and "What did it find?" not in other


async def test_a_question_on_a_task_without_a_screenshot_is_answered_from_its_text(
    rig: Rig,
) -> None:
    rig.model.script = [[say("Where to?"), call("ask_user", question="Where to?")]]
    task = await rig.create_task("Plan a trip", device_ids=[])
    await rig.settle()
    await ask(rig, task.id, uuid4(), "What did it ask?")
    rig.model.script = [[say("Where to go.")]]

    assert await side_chat.answer_open(rig.db, rig.model, rig.objects) == 1  # type: ignore[arg-type]
    assert "input_image" not in str(rig.model.requests[-1].input)
    async with rig.db.tenant(rig.tenant.id) as conn:
        (message,) = await side_chats.for_task(conn, task.id)
    assert message.answer == "Where to go."


async def test_a_question_that_keeps_failing_gets_an_error_not_endless_tries(
    rig: Rig,
) -> None:
    task = await looked_and_said(rig)
    await ask(rig, task.id, uuid4(), "What did it find?")

    class Busy:
        async def respond(self, request: ModelRequest) -> ModelResponse:
            raise ModelError("rate_limited", "busy", retryable=True)

    for _ in range(side_chat.TRIES + 1):
        await side_chat.answer_open(rig.db, Busy(), rig.objects)  # type: ignore[arg-type]
    async with rig.db.tenant(rig.tenant.id) as conn:
        (message,) = await side_chats.for_task(conn, task.id)
    assert message.answer is None and message.error == "rate_limited"
    assert message.tries == side_chat.TRIES


async def test_a_question_that_fails_unexpectedly_does_not_hold_up_the_ones_after_it(
    rig: Rig,
) -> None:
    task = await looked_and_said(rig)
    broken = await ask(rig, task.id, uuid4(), "What broke?")
    later = await ask(rig, task.id, uuid4(), "What did it find?")

    class Flaky:
        async def respond(self, request: ModelRequest) -> ModelResponse:
            if "What broke?" in str(request.input):
                raise RuntimeError("not a model error")
            return ModelResponse(id="r", output=[say("80%.")], usage=None, raw={})

    for _ in range(side_chat.TRIES):
        await side_chat.answer_open(rig.db, Flaky(), rig.objects)  # type: ignore[arg-type]
    async with rig.db.tenant(rig.tenant.id) as conn:
        answers = {m.id: m for m in await side_chats.for_task(conn, task.id)}
    assert answers[later.id].answer == "80%."
    assert answers[broken.id].error == "internal_error"
    assert answers[broken.id].tries == side_chat.TRIES


async def test_closing_a_side_chat_forgets_it(rig: Rig) -> None:
    task = await looked_and_said(rig)
    thread = uuid4()
    await ask(rig, task.id, thread, "What did it find?")
    async with rig.db.tenant(rig.tenant.id) as conn:
        await side_chats.delete_thread(conn, task.id, thread)
        rows = await fetch(conn, "SELECT id FROM side_messages")
    assert rows == []


async def test_without_a_goal_the_agent_does_what_the_messages_ask(rig: Rig) -> None:
    task = await looked_and_said(rig)
    rig.model.script = [[call("ask_user", question="What next?")]]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="/goal clear"
        )
    await rig.settle()
    assert (await rig.task(task.id)).goal == ""
    assert "The user cleared the task's goal" in str(rig.model.requests[-1].input)


async def test_a_paused_goal_is_not_worked_toward_and_its_time_stands_still(rig: Rig) -> None:
    rig.model.script = [[call("ask_user", question="Go on?")]]
    task = await rig.create_task("/goal Compare five shops", device_ids=[])
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="/goal pause"
        )
    rig.model.script = [[call("ask_user", question="Paused, what now?")]]
    await rig.settle()
    assert (await rig.task(task.id)).goal_paused
    assert "The user paused the task's goal" in str(rig.model.requests[-1].input)

    async with rig.db.tenant(rig.tenant.id) as conn:
        detail = await views.task_detail(conn, await tasks.get(conn, task.id))  # type: ignore[arg-type]
    # The agent worked twice (two model calls) but the goal only during the first.
    assert 0 < detail.goal_ms < detail.agent_ms

    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="/goal resume"
        )
    rig.model.script = [[call("ask_user", question="Back on it")]]
    await rig.settle()
    assert not (await rig.task(task.id)).goal_paused
    assert "The user resumed the task's goal" in str(rig.model.requests[-1].input)


async def test_two_workers_answer_a_question_once(rig: Rig) -> None:
    task = await looked_and_said(rig)
    await ask(rig, task.id, uuid4(), "What did it find?")
    release = asyncio.Event()
    calls: list[ModelRequest] = []

    class Slow:
        async def respond(self, request: ModelRequest) -> ModelResponse:
            calls.append(request)
            await release.wait()  # the first worker is still answering
            return ModelResponse(id="r", output=[say("80%.")], usage=None, raw={})

    first = asyncio.create_task(side_chat.answer_open(rig.db, Slow(), rig.objects))  # type: ignore[arg-type]
    while not calls:
        await asyncio.sleep(0.01)
    second = await side_chat.answer_open(rig.db, Slow(), rig.objects)  # type: ignore[arg-type]
    release.set()
    assert (await first, second) == (1, 0)
    assert len(calls) == 1
