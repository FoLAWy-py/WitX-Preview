"""After the model call: record the step, handle its tool calls and move the run on
(``rules.turn_end``); or, when the call failed, retry, fall back or wait for the user."""

import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.domain.model import GoalVerdict, Run, RunStatus, Step, Task, WaitReason
from witx.harness import calls, context
from witx.harness.prepare import RECHECK, wait_reason
from witx.harness.risk import RiskCheck
from witx.harness.turn import Outcome, Prepared, Turn, calls_of, has_goal, reply_text, utcnow
from witx.harness.verify import Judgement
from witx.models import ModelError, ModelResponse
from witx.services import lifecycle
from witx.store import execution, identity, runs, side_turns, tasks

if TYPE_CHECKING:
    from witx.harness.loop import Harness

log = logging.getLogger(__name__)


async def model_failed(harness: "Harness", prepared: Prepared, error: ModelError) -> None:
    async with harness.db.tenant(prepared.task.tenant_id) as conn:
        await tasks.get(conn, prepared.task.id, lock=True)  # before the run, as a stop
        # What the provider answered, if it did, so a failure can be read afterwards.
        await runs.finish_step(
            conn,
            prepared.step_id,
            status="error",
            response=error.raw,
            usage=(error.raw or {}).get("usage"),
            error=str(error)[:2000],
        )
        failed = await runs.failed_models_since_success(conn, prepared.run.id)
        failures = failed.count(prepared.task.model)
        again = error.retryable and failures < harness.limits.model_retries
        fallback = (
            await _fallback(harness, conn, prepared, failed)
            if error.retryable and not again
            else None
        )
        if fallback is not None:
            # The same step again on another model the tenant allows.
            await lifecycle.set_model(
                conn,
                task_id=prepared.task.id,
                model=fallback,
                catalog=harness.catalog,
                fallback_for=error.code,
            )
            await runs.set_state(conn, prepared.run.id, RunStatus.RUNNABLE, epoch=prepared.epoch)
        elif again:
            delay = timedelta(seconds=min(300, 5 * 2**failures))
            await runs.set_state(
                conn,
                prepared.run.id,
                RunStatus.WAITING,
                epoch=prepared.epoch,
                wait_reason=WaitReason.MODEL_RETRY,
                wait_detail=error.code,
                next_wake_at=utcnow() + delay,
            )
        else:
            # Not a task failure: the user can add input or change the model and resume.
            await runs.set_state(
                conn,
                prepared.run.id,
                RunStatus.WAITING,
                epoch=prepared.epoch,
                wait_reason=WaitReason.USER_INPUT,
                wait_detail=f"model error: {error}"[:500],
            )
        await tasks.emit(
            conn,
            prepared.task,
            "run.model_error",
            run_id=prepared.run.id,
            code=error.code,
            # Whether the step is tried again by itself (later, or on another model).
            retrying=again or fallback is not None,
        )


async def _fallback(
    harness: "Harness", conn: AsyncConnection, prepared: Prepared, failed: list[str]
) -> str | None:
    """The model to fall back to, if the tenant's owner turned fallback on."""
    if harness.default_provider is None:
        return None
    if not await identity.model_fallback(conn, prepared.task.tenant_id):
        return None
    return rules.fallback_model(
        current=prepared.task.model,
        failed=set(failed),
        catalog=[(m.name, m.provider) for m in harness.catalog.values()],
        allowed=rules.allowed_providers(prepared.allowed_providers, harness.default_provider),
    )


async def apply(
    harness: "Harness",
    prepared: Prepared,
    response: ModelResponse,
    verdict: tuple[str, RiskCheck] | None = None,
    judgement: Judgement | None = None,
) -> None:
    risk_call = verdict[1].record if verdict else None
    verify_call = judgement.record if judgement else None
    if verify_call is not None and judgement is not None and judgement.verdict:
        # Kept on the step: the agent reads the verdict at its next turn.
        verify_call = verify_call | {
            "goal": {"verdict": judgement.verdict.verdict, "reason": judgement.verdict.reason}
        }
    async with harness.db.tenant(prepared.task.tenant_id) as conn:
        # The task first, then the run: the order a stop or pause takes.
        await tasks.get(conn, prepared.task.id, lock=True)
        run = await runs.get(conn, prepared.run.id, lock=True)
        assert run is not None
        if run.epoch != prepared.epoch or run.status != RunStatus.RUNNING:
            await runs.finish_step(
                conn,
                prepared.step_id,
                status="abandoned",
                response=response.raw,
                risk_call=risk_call,
                verify_call=verify_call,
            )
            return
        await runs.finish_step(
            conn,
            prepared.step_id,
            status="done",
            response=response.raw,
            output_items=response.output,
            usage=response.usage,
            risk_call=risk_call,
            verify_call=verify_call,
        )
        task = await tasks.get(conn, run.task_id)
        assert task is not None
        text = rules.shown_text(reply_text(response))
        if text:
            await tasks.emit(conn, task, "agent.message", run_id=run.id, text=text)
        searched = [i for i in response.output if i.get("type") == "web_search_call"]
        for item in searched:
            # A search names its query; opening a result page names the page.
            action = item.get("action") or {}
            query = str(action.get("query") or "")[:500]
            url = str(action.get("url") or "")[:500]
            if query or url:
                await tasks.emit(
                    conn, task, "agent.web_search", run_id=run.id, query=query, url=url
                )

        function_calls = calls_of(response)
        outcome = Outcome(calls=len(function_calls), goal=judgement.verdict if judgement else None)
        turn = Turn(harness, conn, task, run, outcome, verdict)
        for item in function_calls:
            try:
                args = json.loads(item.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                args = {}
            call = await runs.insert_call(
                conn,
                id=uuid4(),
                tenant_id=task.tenant_id,
                run_id=run.id,
                step_id=prepared.step_id,
                call_id=str(item.get("call_id")),
                name=str(item.get("name")),
                arguments=args,
            )
            await calls.handle(turn, call)

        judged = outcome.goal
        if judged is not None:
            await _goal_judged(
                conn,
                task,
                run,
                outcome,
                judged,
                reply=None if function_calls else text,
                steps=prepared.steps,
            )
        open_calls = [c for c in await runs.calls_for_run(conn, run.id) if c.output is None]
        end = rules.turn_end(
            completed=outcome.completed is not None,
            plan=outcome.plan is not None,
            repeating=outcome.repeating,
            open_calls=bool(open_calls),
            goal_continue=outcome.goal_continue,
            question=outcome.question,
            ask_main=outcome.ask_main,
            called=bool(function_calls),
            reply=text,
            searched=bool(searched),
            # This turn only searched when it searched and called none of our tools.
            in_circles=rules.searching_in_circles(
                [*map(context.only_searched, prepared.steps), bool(searched) and not function_calls]
            ),
        )
        if end.status == RunStatus.COMPLETED:
            assert outcome.completed is not None
            if judged is not None and judged.verdict == "met" and has_goal(task):
                await lifecycle.end_goal(conn, task, "achieved", judged.reason)
            await lifecycle.finish_run(
                conn, run, RunStatus.COMPLETED, outcome.completed, epoch=prepared.epoch
            )
            if run.parent_run_id is None:
                # A message that came while it finished starts the next round.
                await lifecycle.read_the_rest(conn, task.id, prepared.input_seq)
            return
        if end.status == RunStatus.RUNNABLE:
            await runs.set_state(conn, run.id, RunStatus.RUNNABLE, epoch=prepared.epoch)
        elif end.wait_reason is None:
            await runs.set_state(
                conn,
                run.id,
                RunStatus.WAITING,
                epoch=prepared.epoch,
                wait_reason=await wait_reason(conn, open_calls),
                next_wake_at=utcnow() + RECHECK,
            )
        else:
            await runs.set_state(
                conn,
                run.id,
                RunStatus.WAITING,
                epoch=prepared.epoch,
                wait_reason=end.wait_reason,
                wait_detail=end.detail,
            )
        if end.notice == "run.repeating":
            await tasks.emit(conn, task, "run.repeating", run_id=run.id, count=rules.LOOP_ASK)
        if end.question is not None:
            await tasks.emit(
                conn,
                task,
                "agent.question",
                run_id=run.id,
                text=end.question,
                **({"to": "main"} if end.to_main else {}),
                **(
                    {"options": outcome.options, "recommended": outcome.recommended}
                    if end.asked and outcome.options
                    else {}
                ),
            )
        if end.to_main:
            assert run.parent_run_id is not None and end.question is not None
            await side_turns.insert(
                conn,
                id=uuid4(),
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=run.parent_run_id,
                about_run_id=run.id,
                text=end.question,
            )


async def _goal_judged(
    conn: AsyncConnection,
    task: Task,
    run: Run,
    outcome: Outcome,
    judged: GoalVerdict,
    *,
    reply: str | None,
    steps: list[Step],
) -> None:
    """What the independent check of the goal decides for this turn. ``reply`` is the
    agent's words when the turn ended without a tool call (the agent has stopped: met
    completes the run, not met sends it back to work), None at a completion (handled
    by ``_complete``)."""
    turns, _ = await tasks.goal_progress(conn, task.id)
    await tasks.emit(
        conn,
        task,
        "goal.evaluated",
        run_id=run.id,
        verdict=judged.verdict,
        reason=judged.reason,
        turn=turns + 1,
    )
    if judged.verdict == "impossible":
        await lifecycle.end_goal(conn, task, "impossible", judged.reason)
        outcome.question = (
            f"The goal cannot be met: {judged.reason} Tell me how to go on, or give a new goal."
        )
    if reply is None or outcome.question is not None:
        return
    if judged.verdict == "met":
        actions = [a for a in await execution.task_actions(conn, task.id) if a.run_id == run.id]
        if gaps := rules.completion_gaps(actions=actions, evidence=[], device_ids=[]):
            outcome.question = f"The goal looks met, but {'; '.join(gaps)}."
        else:
            outcome.completed = {
                "summary": reply[:4000],
                "evidence_observation_ids": [],
                "completed_at": utcnow().isoformat(),
            }
    elif context.goal_streak(steps) + 1 >= rules.GOAL_STALL:
        outcome.question = (
            f"The goal is still not met: {judged.reason} Tell me how to go on, or change the goal."
        )
    else:
        outcome.goal_continue = True
