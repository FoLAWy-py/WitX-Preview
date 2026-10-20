"""Long runs: earlier steps give way to a summary while what must not be
lost stays word for word; the agent's plan and notes stay in view."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from witx.api import views
from witx.config import Limits
from witx.domain import rules
from witx.domain.model import RunSummary, Step
from witx.harness import context
from witx.harness.compress import Compression, transcript
from witx.store import tasks
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, now, provision
from tests.cloud.test_flow import complete, tap


def point(seqs: list[int], **kw: Any) -> int | None:
    return rules.compression_point(
        seqs,
        last_attempt=kw.get("last_attempt"),
        last_input_tokens=kw.get("tokens"),
        after_steps=kw.get("after", 6),
        at_tokens=kw.get("at", 25_000),
        keep_steps=kw.get("keep", 2),
    )


def test_earlier_steps_are_summarized_after_enough_steps_or_a_large_input() -> None:
    assert point(list(range(1, 7))) is None  # five steps after the first: not yet
    assert point(list(range(1, 8))) == 5  # the newest two stay word for word
    assert point(list(range(1, 9)), tokens=30_000) == 6  # a large input, sooner
    assert point([1, 2, 3, 4, 5, 6], tokens=30_000) is None  # too little to take in


def test_a_failed_summary_is_not_retried_at_every_step() -> None:
    assert point(list(range(1, 11)), last_attempt=5) is None  # five steps since it
    assert point(list(range(1, 12)), last_attempt=5) == 9


def turn(seq: int, *, notes: str | None = None, update: str | None = None) -> Step:
    """Step ``seq``: its input answers step seq-1's call; its output calls observe."""
    items: list[dict[str, Any]] = []
    if seq == 1:
        items.append({"role": "user", "content": [{"type": "input_text", "text": "The task"}]})
    else:
        items.append({"type": "function_call_output", "call_id": f"c{seq - 1}", "output": "{}"})
    if update:
        text = f"{context.UPDATE_PREFIX}\n- [note] {update}"
        items.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
    args = {"device_id": "d", "observation_id": f"o{seq}", "notes": notes}
    output = [
        {
            "type": "function_call",
            "call_id": f"c{seq}",
            "name": "tap",
            "arguments": json.dumps(args),
        }
    ]
    return Step(uuid4(), uuid4(), seq, "done", 0, items, output)


def test_a_summary_keeps_the_task_the_user_s_messages_notes_and_every_call_s_result() -> None:
    steps = [
        turn(1),
        turn(2, notes="Battery 80%"),
        turn(3, update="Only look, change nothing"),
        turn(4),
        turn(5),
        turn(6),
    ]
    summary = RunSummary(uuid4(), uuid4(), 4, "Read the battery level.", now())
    items = context.history(steps, summary, ["tap on 'Pay' (device d, action a)"])
    text = json.dumps(items, ensure_ascii=False)
    assert items[0]["content"][0]["text"] == "The task"
    assert "Read the battery level." in text
    assert "Battery 80% (screenshot o2, step 2)" in text  # the note, word for word
    assert "Only look, change nothing" in text  # the user's message, word for word
    assert "tap on 'Pay'" in text  # an input with an unknown effect
    calls = [i["call_id"] for i in items if i.get("type") == "function_call"]
    results = [i["call_id"] for i in items if i.get("type") == "function_call_output"]
    assert calls == ["c4", "c5", "c6"] and results == ["c4", "c5"]  # c6: answered next


def test_the_record_given_to_the_summary_names_screenshots_and_skips_reasoning() -> None:
    record = transcript(
        [
            {"type": "function_call", "name": "observe", "arguments": '{"device_id": "d"}'},
            {"type": "reasoning", "encrypted_content": "x"},
            {"role": "user", "content": [{"type": "input_image", context.IMAGE_PLACEHOLDER: "o9"}]},
        ]
    )
    assert "Agent called observe" in record and "[screenshot o9]" in record
    assert "encrypted" not in record


@dataclass
class FakeCompressor:
    summary: str | None
    asked: list[list[dict[str, Any]]] = field(default_factory=list)

    async def summarize(self, **kw: Any) -> Compression:
        self.asked.append(kw["items"])
        return Compression(self.summary, {"usage": {"input_tokens": 500, "output_tokens": 50}})


async def long_run(
    db: Database, admin_db: Database, tmp_path: Path, summary: str | None
) -> tuple[Rig, Any, FakeCompressor]:
    tenant = await provision(admin_db, "acme", devices=1)
    compressor = FakeCompressor(summary)
    limits = Limits(compress_after_steps=4, compress_keep_steps=2)
    rig = Rig(db, tenant, tmp_path, limits, compressor=compressor)
    await rig.connect([device_info(0)])
    device = tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)] for _ in range(9)] + [complete]
    task = await rig.create_task("Check the battery, step by step")
    await rig.settle(rounds=60)
    return rig, task, compressor


async def test_a_long_run_carries_a_summary_instead_of_its_earlier_steps(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, compressor = await long_run(db, admin_db, tmp_path, "Observed the phone 4 times.")
    assert compressor.asked
    later = [str(r.input) for r in rig.model.requests if "Observed the phone" in str(r.input)]
    assert later, "requests after the summary carry it"
    # The task as the run began is still there; the steps before the summary are not.
    assert all("Check the battery, step by step" in r for r in later)
    last = rig.model.requests[-1].input
    calls = [i for i in last if i.get("type") == "function_call"]
    # The run called observe nine times; those before the summary's last step are gone.
    assert len(calls) < 9
    assert (await rig.task(task.id)).status.value == "completed"
    assert (await rig.usage(task.id))["input_tokens"] >= 500  # the summary call counts


async def test_a_failed_summary_leaves_the_history_whole(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task, compressor = await long_run(db, admin_db, tmp_path, None)
    assert compressor.asked  # tried, failed
    assert not any("summarized by WitX" in str(r.input) for r in rig.model.requests)
    assert (await rig.task(task.id)).status.value == "completed"


def test_a_plan_has_short_steps_and_at_most_one_in_progress() -> None:
    plan = rules.check_plan([{"step": "  Open   Settings ", "status": "done"}])
    assert plan == [{"step": "Open Settings", "status": "done"}]
    for bad in (
        [],
        [{"step": "a", "status": "in_progress"}, {"step": "b", "status": "in_progress"}],
        [{"step": "", "status": "pending"}],
        [{"step": "a", "status": "later"}],
    ):
        with pytest.raises(rules.Rejected, match="invalid_plan"):
            rules.check_plan(bad)


def test_the_plan_stays_word_for_word_when_earlier_steps_are_summarized() -> None:
    plan_call = {
        "type": "function_call",
        "call_id": "p",
        "name": "update_plan",
        "arguments": json.dumps({"steps": [{"step": "Read the version", "status": "in_progress"}]}),
    }
    steps = [turn(1), turn(2), turn(3), turn(4)]
    steps[1].output_items.append(plan_call)
    summary = RunSummary(uuid4(), uuid4(), 3, "Did things.", now())
    text = json.dumps(context.history(steps, summary), ensure_ascii=False)
    assert "- [in_progress] Read the version" in text


async def test_the_task_shows_the_newest_plan_and_what_the_agent_wrote_down(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    device = tenant.device_ids[0]
    first = [{"step": "Look", "status": "in_progress"}, {"step": "Report", "status": "pending"}]
    second = [{"step": "Look", "status": "done"}, {"step": "Report", "status": "in_progress"}]
    rig.model.script = [
        [call("update_plan", steps=first)],
        lambda r: tap(r, device, notes="Battery 80%"),
        [call("update_plan", steps=[{"step": "x", "status": "maybe"}])],  # refused
        [call("update_plan", steps=second)],
        complete,
    ]
    task = await rig.create_task("Check the battery and report it")
    await rig.settle()
    async with rig.db.tenant(tenant.id) as conn:
        detail = await views.task_detail(conn, await tasks.get(conn, task.id))  # type: ignore[arg-type]
    assert [p.model_dump() for p in detail.plan] == second
    assert [n.text for n in detail.notes] == ["Battery 80%"]
    assert detail.notes[0].observation_id is not None and detail.notes[0].seen_at is not None
