"""An agent offers answers with its question: a few short options and the one it
recommends, which the Web app shows as buttons."""

from pathlib import Path

import pytest
from witx.domain import rules
from witx.store import tasks
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision


def test_two_to_four_short_options_are_kept_with_the_recommended_one() -> None:
    options = ["Shanghai Disneyland", " Tokyo Disneyland ", "Shanghai Disneyland", "", 7, "Paris"]
    assert rules.ask_options(options, 1) == (
        ["Shanghai Disneyland", "Tokyo Disneyland", "Paris"],
        1,
    )
    many = [str(i) for i in range(9)]
    assert rules.ask_options(many, 0)[0] == ["0", "1", "2", "3"]
    assert rules.ask_options(["x" * 500, "y"], None)[0][0] == "x" * rules.ASK_OPTION_CHARS


@pytest.mark.parametrize(
    ("options", "recommended"),
    [(["only one"], 0), ([], None), (None, 0), ("two, three", 1), ([" ", ""], 0)],
)
def test_fewer_than_two_options_are_no_choice(options: object, recommended: object) -> None:
    assert rules.ask_options(options, recommended) == ([], None)


@pytest.mark.parametrize("recommended", [2, -1, True, "1", 0.5, None])
def test_a_recommendation_that_is_not_one_of_the_options_is_dropped(recommended: object) -> None:
    assert rules.ask_options(["a", "b"], recommended)[1] is None


async def asked(
    db: Database, admin_db: Database, tmp_path: Path, **arguments: object
) -> dict[str, object]:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    rig.model.script = [[call("ask_user", **arguments)]]
    task = await rig.create_task("Navigate to Disneyland")
    await rig.settle()
    async with rig.db.tenant(tenant.id) as conn:
        events = [e for e in await tasks.events_after(conn, task.id, 0)]
    (question,) = [e for e in events if e["type"] == "agent.question"]
    return dict(question["data"])


async def test_the_question_event_carries_the_options_and_the_recommendation(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    data = await asked(
        db,
        admin_db,
        tmp_path,
        question="Which Disneyland?",
        options=["Shanghai", "Tokyo"],
        recommended=1,
    )
    assert data["text"] == "Which Disneyland?"
    assert (data["options"], data["recommended"]) == (["Shanghai", "Tokyo"], 1)


async def test_an_open_question_has_no_options_in_its_event(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    data = await asked(
        db, admin_db, tmp_path, question="What is the code?", options=[], recommended=None
    )
    assert "options" not in data and "recommended" not in data
