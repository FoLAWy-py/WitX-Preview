"""The main agent's side turns while its sub-agents work.

The main agent waits in its delegate call until every sub-agent has finished. A
sub-agent's question for it (ask_user with to "main"), or the user's message to it,
starts a side turn: one model call on the main agent's history as it stands (the same
instructions and tools, so the provider's cache of that prefix holds), with the
delegate call still working and the question or message after it. In it the main agent
can answer a sub-agent or pass something on (message_agent), put a question to the user
instead (ask_user true), or stop a sub-agent (stop_agent). The side turn is no step of
the main agent's run: its history does not change, and once its sub-agents finish it
reads what passed while they worked.

What the main agent tells a sub-agent is information, not the user's word: the
sub-agent reads it as from the main agent, and it grants no permission. One worker at a
time takes side turns, oldest first, so none is paid for twice.
"""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection
from witx_contracts import logs

from witx.domain import rules
from witx.domain.model import (
    TERMINAL_RUN,
    ActionStatus,
    Effect,
    Run,
    RunStatus,
    SideTurn,
    Task,
    TaskStatus,
    WaitReason,
)
from witx.harness import context
from witx.models import ModelError, ModelResponse
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.store import devices, execution, identity, runs, side_turns, tasks

if TYPE_CHECKING:
    from witx.harness.loop import Harness

log = logging.getLogger(__name__)

TRIES = 3  # a side turn that fails this often ends; a question then goes to the user

HEADER = (
    "[Side turn while your sub-agents work. Only message_agent and stop_agent work now; "
    "your own work goes on when they have all finished.]"
)


async def take_open(harness: "Harness") -> int:
    """Takes the open side turns of every tenant; returns how many it tried."""
    async with harness.db.only_one("side_turns") as mine:
        if not mine:
            return 0
        async with harness.db.system() as conn:
            tenants = [row["id"] for row in await identity.tenants(conn)]
        tried = 0
        for tenant_id in tenants:
            with logs.log_context(tenant_id=tenant_id):
                async with harness.db.tenant(tenant_id) as conn:
                    todo = await side_turns.open_turns(conn)
                for turn in todo:
                    try:
                        await _take(harness, tenant_id, turn)
                    except Exception:
                        # Not the model: counted like a failed try, so one bad side turn
                        # does not hold up the others, and in the end goes to the user.
                        log.exception("side turn %s failed", turn.id)
                        await _failed(harness, tenant_id, turn, "error", None)
                    tried += 1
        return tried


def _now() -> datetime:
    return datetime.now(UTC)


async def _failed(
    harness: "Harness", tenant_id: UUID, turn: SideTurn, code: str, kept: dict[str, Any] | None
) -> None:
    """A failed try; the last one ends the side turn, and its question goes to the user."""
    async with harness.db.tenant(tenant_id) as conn:
        call = {"request": kept} if kept is not None else None
        if turn.tries + 1 < TRIES:
            await side_turns.failed_try(conn, turn.id, call)
            return
        await side_turns.finish(conn, turn.id, _now(), call=call, error=code)
        task = await tasks.get(conn, turn.task_id)
        asker = await runs.get(conn, turn.about_run_id) if turn.about_run_id else None
        if task is not None and asker is not None:
            await _to_user(conn, task, await runs.get(conn, turn.run_id), asker)


async def _take(harness: "Harness", tenant_id: UUID, turn: SideTurn) -> None:
    async with harness.db.tenant(tenant_id) as conn:
        task = await tasks.get(conn, turn.task_id)
        main = await runs.get(conn, turn.run_id)
        asker = await runs.get(conn, turn.about_run_id) if turn.about_run_id else None
        if task is None or main is None or not await _delegating(conn, task, main):
            # The sub-agents finished meanwhile: the main agent reads it all on its own.
            await side_turns.finish(conn, turn.id, _now(), call=None, error="moot")
            if asker is not None and task is not None:
                await _to_user(conn, task, main, asker)
            return
        if asker is not None and not (_asks_main(asker) and asker.wait_detail == turn.text[:1000]):
            # Answered, stopped, or asking something newer (with a side turn of its own).
            await side_turns.finish(conn, turn.id, _now(), call=None, error="moot")
            return
        steps = await runs.completed_steps(conn, main.id)
        calls = await runs.calls_for_run(conn, main.id)
        attempts = await runs.latest_summaries(conn, main.id)
        summary = next((s for s in reversed(attempts) if s.summary), None)
        unknown = [
            f"{a.kind} on {a.target!r} (device {a.device_id}, action {a.id})"
            for a in await execution.task_actions(conn, task.id)
            if a.run_id == main.id and a.effect == Effect.UNKNOWN
        ]
        text = await _prompt(conn, task, main, turn, asker, steps[-1].input_seq if steps else 0)
        chosen = await identity.allowed_providers(conn, task.tenant_id)
    added = context.side_turn(steps, calls, text)
    items = context.history(steps, summary, unknown) + added
    allowed = None if chosen is None else tuple(chosen)
    request = await harness.request_for(task, items, steps, allowed)
    kept: dict[str, Any] = {"model": task.model, "history_steps": len(steps), "input": added}
    try:
        response = await harness.model.respond(request)
    except ModelError as error:
        log.info("side turn failed: %s", error.code)
        last = turn if error.retryable else replace(turn, tries=TRIES)  # no retry
        await _failed(harness, tenant_id, last, error.code, kept)
        return
    async with harness.db.tenant(tenant_id) as conn:
        # The task first, then the side turn: the order a withdrawal takes.
        await tasks.get(conn, task.id, lock=True)
        locked = await side_turns.get(conn, turn.id, lock=True)
        if locked is None or locked.done_at is not None:
            return
        done = await _apply(conn, task, main, turn, response)
        await side_turns.finish(
            conn,
            turn.id,
            _now(),
            call={
                "request": kept,
                "response": response.raw,
                "usage": response.usage,
                "applied": done,
            },
        )


async def agent_names(conn: AsyncConnection, run: Run) -> dict[UUID, str]:
    """The run's sub-agents by ID, each by its name, else its phone's, else its number."""
    names = {}
    phones = {d.id: d.name for d in await devices.list_all(conn)}
    children = sorted(
        await runs.children(conn, run.id),
        key=lambda c: (c.created_at, int(str(c.spawn_key or "0:0").rsplit(":", 1)[-1])),
    )
    for n, child in enumerate(children, start=1):
        phone = phones.get(child.device_ids[0]) if child.device_ids else None
        names[child.id] = child.name or (f"the agent on {phone}" if phone else f"sub-agent {n}")
    return names


async def _delegating(conn: AsyncConnection, task: Task, main: Run) -> bool:
    """The main agent still waits for its sub-agents (also while a worker rechecks it)."""
    return (
        task.status == TaskStatus.ACTIVE
        and main.status not in TERMINAL_RUN
        and await runs.delegating(conn, main.id)
    )


def _asks_main(run: Run) -> bool:
    return run.status == RunStatus.WAITING and run.wait_reason == WaitReason.AGENT


async def _prompt(
    conn: AsyncConnection,
    task: Task,
    main: Run,
    turn: SideTurn,
    asker: Run | None,
    read_through: int,
) -> str:
    """What the side turn is about, with what the main agent needs to answer it."""
    names = await agent_names(conn, main)
    children = {c.id: c for c in await runs.children(conn, main.id)}
    parts = [HEADER]
    if asker is not None:
        options = await _options(conn, asker)
        parts.append(
            f"{names.get(asker.id, 'A sub-agent')} (agent {asker.id}) asks you: {turn.text}"
        )
        if options[0]:
            parts.append(
                "Answers it offers: "
                + "; ".join(
                    o + (" (it recommends this)" if n == options[1] else "")
                    for n, o in enumerate(options[0])
                )
            )
        parts.append(
            "Answer it with message_agent if you can tell from this task: what the user "
            "said, what you found, the code, the devices. If only the user can answer it "
            "(a preference, a choice, an account, whether to go on), call message_agent "
            "with ask_user true and no text."
        )
    else:
        parts.append(f"The user wrote to you: {turn.text}")
        parts.append(
            "Your sub-agents do not read the user's messages. If this concerns one of them, "
            "pass on what it needs with message_agent; if one should not go on, stop it "
            "with stop_agent. Otherwise call no tool: you read the message again when they "
            "have finished."
        )
    working = [
        f"- {c.id}: {names[c.id]}, {c.status.value}"
        + (", waiting for your answer" if _asks_main(c) else "")
        + (", waiting for the user" if c.wait_reason == WaitReason.USER_INPUT else "")
        for c in children.values()
        if c.status not in TERMINAL_RUN
    ]
    parts.append("Your sub-agents still working:\n" + ("\n".join(working) or "- none"))
    # Other messages of the user have side turns of their own: not read here, so they
    # stay withdrawable until one reads them.
    inputs = [dict(i) for i in await tasks.inputs(conn, task.id) if i["seq"] > read_through]
    passed = context.relayed_lines(
        names, [dict(q) for q in await tasks.questions(conn, task.id)], inputs
    )
    if passed:
        parts.append("So far while they worked:\n" + "\n".join(passed))
    return "\n\n".join(parts)


async def _options(conn: AsyncConnection, run: Run) -> tuple[list[str], int | None]:
    """The answers a run offered with its newest question, and the one it recommends."""
    asked = [c for c in await runs.calls_for_run(conn, run.id) if c.name == "ask_user"]
    if not asked:
        return [], None
    args = asked[-1].arguments
    return rules.ask_options(args.get("options"), args.get("recommended"))


async def _apply(
    conn: AsyncConnection, task: Task, main: Run, turn: SideTurn, response: ModelResponse
) -> list[dict[str, Any]]:
    """Carries out the side turn's calls; returns what was done, for the record. The
    task is locked first, as lifecycle does (inputs are numbered under that lock)."""
    locked = await tasks.get(conn, task.id, lock=True)
    if locked is None or not await _delegating(conn, locked, main):
        return [{"status": "moot"}]
    children = {c.id: c for c in await runs.children(conn, main.id)}
    done: list[dict[str, Any]] = []
    answered = False
    for item in response.output:
        if item.get("type") != "function_call":
            continue
        try:
            args = json.loads(item.get("arguments") or "{}")
            agent_id = UUID(str(args.get("agent_id")))
        except (ValueError, AttributeError):
            done.append({"call": item.get("name"), "status": "rejected", "code": "unknown_agent"})
            continue
        child = children.get(agent_id)
        if child is not None:
            child = await runs.get(conn, child.id, lock=True)
        if child is None or child.status in TERMINAL_RUN:
            done.append({"call": item.get("name"), "status": "rejected", "code": "unknown_agent"})
            continue
        name = str(item.get("name"))
        if name == "message_agent" and args.get("ask_user"):
            if _asks_main(child):
                await _to_user(conn, task, main, child)
                answered = answered or child.id == turn.about_run_id
            done.append({"call": name, "agent_id": str(child.id), "status": "asked_user"})
        elif name == "message_agent" and str(args.get("text") or "").strip():
            text = rules.shown_text(str(args["text"]).strip())[:4000]
            await _tell(conn, task, main, child, text, answer=child.id == turn.about_run_id)
            answered = answered or child.id == turn.about_run_id
            done.append({"call": name, "agent_id": str(child.id), "status": "sent"})
        elif name == "stop_agent":
            reason = rules.shown_text(str(args.get("reason") or "").strip())[:300]
            if await _input_in_flight(conn, task, child):
                # A stop counts only once the phone confirmed it (AGENTS.md): not here.
                done.append({"call": name, "agent_id": str(child.id), "code": "input_in_flight"})
                continue
            await _stop(conn, task, main, child, reason)
            done.append({"call": name, "agent_id": str(child.id), "status": "stopped"})
        else:
            done.append({"call": name, "status": "rejected", "code": "not_in_side_turn"})
    asker = children.get(turn.about_run_id) if turn.about_run_id else None
    if asker is not None and not answered:
        asker = await runs.get(conn, asker.id, lock=True)
        said = rules.shown_text(
            "\n".join(
                part.get("text", "")
                for item in response.output
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ).strip()
        )[:4000]
        if asker is not None and _asks_main(asker):
            if said:  # words without a tool call answer the question
                await _tell(conn, task, main, asker, said, answer=True)
                done.append({"call": None, "agent_id": str(asker.id), "status": "sent"})
            else:
                await _to_user(conn, task, main, asker)
                done.append({"call": None, "agent_id": str(asker.id), "status": "asked_user"})
    return done


async def _tell(
    conn: AsyncConnection, task: Task, main: Run, child: Run, text: str, *, answer: bool
) -> None:
    """The main agent's word to a sub-agent: it reads it on its next step."""
    seq = await tasks.add_input(
        conn,
        tenant_id=task.tenant_id,
        task_id=task.id,
        kind="agent",
        text=text,
        revision=task.revision,
        created_by=None,
        run_id=child.id,
    )
    await tasks.emit(
        conn,
        task,
        "agent.relay",
        run_id=main.id,
        to_run_id=child.id,
        kind="answer" if answer else "message",
        text=text,
        seq=seq,
    )
    # It wakes a sub-agent that asked, not one that stopped for the user to decide.
    if child.wait_reason == WaitReason.AGENT or (
        child.wait_reason == WaitReason.USER_INPUT and rules.asked_question(child.wait_detail)
    ):
        await runs.wake(conn, child.id, (WaitReason.AGENT, WaitReason.USER_INPUT))


async def _to_user(conn: AsyncConnection, task: Task, main: Run | None, child: Run) -> None:
    """A sub-agent's question for the main agent goes to the user instead."""
    child_now = await runs.get(conn, child.id, lock=True)
    if child_now is None or not _asks_main(child_now):
        return
    question = child_now.wait_detail or ""
    await runs.set_state(
        conn,
        child.id,
        RunStatus.WAITING,
        wait_reason=WaitReason.USER_INPUT,
        wait_detail=question,
    )
    options, recommended = await _options(conn, child_now)
    await tasks.emit(
        conn,
        task,
        "agent.question",
        run_id=child.id,
        text=question,
        to="user",
        via="main",
        **({"options": options, "recommended": recommended} if options else {}),
    )
    if main is not None:
        await tasks.emit(
            conn, task, "agent.relay", run_id=main.id, to_run_id=child.id, kind="ask_user"
        )


async def _input_in_flight(conn: AsyncConnection, task: Task, child: Run) -> bool:
    return any(
        a.run_id == child.id and a.status == ActionStatus.SENT
        for a in await execution.task_actions(conn, task.id)
    )


async def _stop(conn: AsyncConnection, task: Task, main: Run, child: Run, reason: str) -> None:
    """Ends a sub-agent: its unsent inputs are cancelled and its devices go back."""
    await exec_service.cancel_unsent(conn, task.id, "agent_stopped", child.id)
    summary = "Stopped by the main agent" + (f": {reason}" if reason else ".")
    await lifecycle.finish_run(conn, child, RunStatus.CANCELLED, {"summary": summary})
    await tasks.emit(
        conn, task, "agent.relay", run_id=main.id, to_run_id=child.id, kind="stop", text=reason
    )
