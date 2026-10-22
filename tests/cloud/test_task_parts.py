"""A long task's page loads in parts: the latest first, earlier ones as it scrolls."""

from pathlib import Path
from typing import Any

import pytest
from witx.api import views
from witx.domain import rules
from witx.services import lifecycle
from witx.store import tasks
from witx.store.db import Database
from witx_contracts.api import TaskDetail

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision, say

KINDS = {"task.created", "agent.message", "input.added"}


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


@pytest.fixture(autouse=True)
def one_round_a_part(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "PART_ROUNDS", 1)


def tap(request: Any, device: str, n: int) -> list[dict[str, Any]]:
    return [
        call(
            "tap",
            device_id=device,
            observation_id=last_screenshot(request),
            x=540,
            y=200 + n * 100,
            target=f"row {n}",
            expected_effect="it opens",
            effects=[],
            notes=f"row {n} was on screen",
        )
    ]


def done(request: Any) -> list[dict[str, Any]]:
    return [
        call("complete_task", summary="done", evidence_observation_ids=[last_screenshot(request)])
    ]


async def round_of(rig: Rig, task_id: Any, text: str) -> None:
    """The person writes again: the finished task goes on in a new round."""
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(conn, task_id=task_id, user_id=rig.tenant.user_id, text=text)
    await rig.settle()


async def page(rig: Rig, task_id: Any, start: Any = "latest") -> TaskDetail:
    async with rig.db.tenant(rig.tenant.id) as conn:
        task = await tasks.get(conn, task_id)
        assert task is not None
        return await views.task_detail(conn, task, start=start, types=KINDS)


async def three_rounds(rig: Rig) -> Any:
    device = str(rig.tenant.device_ids[0])
    rig.model.script = [
        [say("Looking."), call("observe", device_id=device)],
        lambda r: tap(r, device, 0),
        lambda r: tap(r, device, 1),
        done,
        lambda r: tap(r, device, 2),
        done,
        lambda r: tap(r, device, 3),
        done,
    ]
    task = await rig.create_task()
    await rig.settle()
    await round_of(rig, task.id, "And the next row")
    await round_of(rig, task.id, "And one more")
    return task.id


async def test_the_parts_of_a_long_task_put_together_are_the_whole_task(rig: Rig) -> None:
    task_id = await three_rounds(rig)
    whole = await page(rig, task_id, start=None)
    assert whole.earlier is None and len(whole.actions) == 4

    latest = await page(rig, task_id)
    parts = [latest]
    async with rig.db.tenant(rig.tenant.id) as conn:
        task = await tasks.get(conn, task_id)
        assert task is not None
        while parts[-1].earlier is not None:
            parts.append(await views.task_earlier(conn, task, parts[-1].earlier.before, KINDS))

    assert len(parts) == 3  # one round each
    assert [a.id for a in latest.actions] == [whole.actions[-1].id]
    for kind in ("actions", "observations", "events"):
        got = {r.model_dump_json() for p in parts for r in getattr(p, kind)}
        assert got == {r.model_dump_json() for r in getattr(whole, kind)}, kind
    # Notes, oldest part first, are the whole task's notes in order.
    assert [n for p in reversed(parts) for n in p.notes] == whole.notes
    assert {e.type for e in whole.events} <= KINDS and whole.events


async def test_an_action_of_unknown_effect_comes_with_the_latest_part_however_old(
    rig: Rig,
) -> None:
    device = str(rig.tenant.device_ids[0])
    rig.gateway.outcome = "unknown"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, 0),
        [call("ask_user", question="Did the tap happen?")],
        lambda r: [say("Here is another thing."), *done(r)],
        lambda r: [say("And another."), *done(r)],
    ]
    task = await rig.create_task()
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.stop(conn, task.id)
    await rig.settle()
    await round_of(rig, task.id, "Something else")
    await round_of(rig, task.id, "And then")

    latest = await page(rig, task.id)

    assert [r.status for r in latest.runs] == ["cancelled", "completed", "completed"]
    assert latest.earlier is not None
    (unsettled,) = latest.actions
    assert unsettled.effect == "unknown" and unsettled.created_at < latest.earlier.before
    shown = {o.id for o in latest.observations}
    assert unsettled.observation_id in shown  # what the person compares it with


async def test_a_page_fetched_again_from_where_it_was_scrolled_keeps_that_part(rig: Rig) -> None:
    task_id = await three_rounds(rig)
    latest = await page(rig, task_id)
    assert latest.earlier is not None
    async with rig.db.tenant(rig.tenant.id) as conn:
        task = await tasks.get(conn, task_id)
        assert task is not None
        earlier = await views.task_earlier(conn, task, latest.earlier.before, KINDS)
    assert earlier.earlier is not None

    again = await page(rig, task_id, start=earlier.earlier.before)

    assert len(again.actions) == len(latest.actions) + len(earlier.actions)
    assert again.earlier == earlier.earlier
