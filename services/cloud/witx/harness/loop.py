"""One step of a run: settle pending tool calls, call the model once, apply its decisions.

A step is split into two short transactions around the model call, so no database
lock is held while the model thinks. The run epoch fences both: if another worker
claimed the run in between, the late response is recorded as abandoned and ignored.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

from witx import telemetry
from witx.config import CatalogModel, Limits
from witx.domain.model import Step, Task
from witx.harness import apply, checks, context, prepare, skills, tools
from witx.harness.compress import Compressor
from witx.harness.plugins import Installed
from witx.harness.prompt import INSTRUCTIONS
from witx.harness.risk import RiskAssessor
from witx.harness.skills import Skill
from witx.harness.turn import Prepared, calls_of
from witx.harness.verify import GoalJudge
from witx.models import ModelClient, ModelError, ModelRequest
from witx.objects import ObjectStore
from witx.store import execution, runs
from witx.store.db import Database

log = logging.getLogger(__name__)

# What the agent is saying is written for whoever watches at most this often. A
# short message arrives whole within a second, and a step without a risk check is over
# a moment later: at one second the words were cleared before they were ever written.
SAYING_INTERVAL = 0.3


class Harness:
    def __init__(
        self,
        db: Database,
        model: ModelClient,
        objects: ObjectStore,
        limits: Limits,
        risk: RiskAssessor | None = None,
        *,
        catalog: Mapping[str, CatalogModel] | None = None,
        default_model: str | None = None,
        judge: GoalJudge | None = None,
        compressor: Compressor | None = None,
        plugins: Installed | None = None,
    ) -> None:
        self.db = db
        self.model = model
        self.objects = objects
        self.limits = limits
        self.risk = risk  # None: every assessed input counts as a failed assessment
        self.judge = judge  # None: goals are not judged
        self.compressor = compressor  # None: the history is never summarized
        self.plugins = plugins  # None: no skills
        # The models a task may fall back to, in order ; without them, none.
        self.catalog = dict(catalog or {})
        self.default_provider = (
            self.catalog[default_model].provider if default_model in self.catalog else None
        )

    def skills(self) -> dict[str, Skill]:
        """The skills the agent may load now, from the installed plugins."""
        return self.plugins.skills() if self.plugins else {}

    async def step(self, run_id: UUID, tenant_id: UUID, epoch: int) -> None:
        with telemetry.span("agent.step", tenant_id=tenant_id, run_id=run_id):
            await self._step(run_id, tenant_id, epoch)

    async def _step(self, run_id: UUID, tenant_id: UUID, epoch: int) -> None:
        prepared = await prepare.prepare(self, run_id, tenant_id, epoch)
        if prepared is None:
            return
        prepared = await prepare.compress(self, prepared)
        started = time.monotonic()
        saying = Saying(self.db, tenant_id, run_id, epoch)
        rejected = False
        try:
            rejected = await self._call_and_apply(prepared, started, saying)
        finally:
            # Its words stay up until the step is applied and what it said is on record,
            # so what the page shows live is replaced, not blanked, by the message kept.
            # A summary the check refused stays up, marked, while the agent works on.
            if rejected:
                await saying.at("rejected")
            else:
                await saying.end()

    async def _call_and_apply(self, prepared: Prepared, started: float, saying: "Saying") -> bool:
        """Calls the model and applies its decisions; whether its completion was refused
        (the goal is not met yet, or a rule such as fresh evidence was not kept)."""
        try:
            request = replace(await self._request(prepared), on_text=saying)
            with telemetry.span(
                "agent.model_call",
                task_id=prepared.task.id,
                step_id=prepared.step_id,
                model=prepared.task.model,
            ):
                response = await self.model.respond(request)
        except ModelError as error:
            log.warning("model call failed: %s", error, extra={"error_code": error.code})
            await apply.model_failed(self, prepared, error)
            return False
        log.info(
            "model call",
            extra={
                "model": prepared.task.model,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "input_tokens": (response.usage or {}).get("input_tokens"),
                "output_tokens": (response.usage or {}).get("output_tokens"),
            },
        )
        calls = calls_of(response)
        if any(c.get("name") in tools.INPUT_TOOLS for c in calls):
            await saying.at("assessing")
        with telemetry.span("agent.risk_check", task_id=prepared.task.id):
            verdict = await checks.assess(self, prepared, response)
        finishing = any(c.get("name") == "complete_task" for c in calls)
        task, run = prepared.task, prepared.run
        judging = (
            self.judge is not None
            and run.parent_run_id is None
            and bool(task.goal)
            and not task.goal_paused
            and (finishing or not calls)
        )
        if judging:
            await saying.at("checking")
        with telemetry.span("agent.goal_judgement", task_id=task.id):
            judgement = await checks.judge_goal(self, prepared, response)
        await apply.apply(self, prepared, response, verdict, judgement)
        if not finishing:
            return False
        async with self.db.tenant(prepared.task.tenant_id) as conn:
            recorded = await runs.calls_for_run(conn, prepared.run.id)
        return any(
            c.step_id == prepared.step_id
            and c.name == "complete_task"
            and (c.output or {}).get("status") == "rejected"
            for c in recorded
        )

    async def _request(self, prepared: Prepared) -> ModelRequest:
        return await self.request_for(
            prepared.task, prepared.items, prepared.steps, prepared.allowed_providers
        )

    async def request_for(
        self,
        task: Task,
        items: list[dict[str, Any]],
        steps: list[Step],
        allowed_providers: tuple[str, ...] | None,
    ) -> ModelRequest:
        """A request on the run's history: the same instructions and tools every time,
        so the provider's cache of the history's prefix holds (also for side turns)."""

        async def load(obs_id: UUID) -> bytes:
            async with self.db.tenant(task.tenant_id) as conn:
                obs = await execution.get_observation(conn, obs_id)
            assert obs is not None and obs.object_key is not None
            return await self.objects.get(obs.object_key)

        expanded, _ = await context.expand_images(items, load, self.limits.context_images)
        return ModelRequest(
            model=task.model,
            instructions=INSTRUCTIONS + skills.instructions(self.skills()),
            input=expanded,
            tools=tools.TOOLS if self.limits.web_search else tools.TOOLS_WITHOUT_WEB,
            allowed_providers=allowed_providers,
            reasoning_effort=task.reasoning_effort,
            # Off until the agent turned it on for this task (use_web_search, D27).
            web_search=self.limits.web_search and context.web_search_on(steps),
        )


class Saying:
    """Where a step puts the agent's words as they come, and what it is at: on
    the run, fenced by the claim's epoch. Words are written at most every
    SAYING_INTERVAL while they grow, the newest at the interval's end; a phase is
    written at once. A write that fails is let go: this is for whoever watches, never
    for the step."""

    def __init__(self, db: Database, tenant_id: UUID, run_id: UUID, epoch: int) -> None:
        self._db, self._tenant_id, self._run_id, self._epoch = db, tenant_id, run_id, epoch
        self._text: str | None = None
        self._phase: str | None = None
        self._last = 0.0
        self._pending: tuple[str, str] | None = None  # held back by the interval
        self._later: asyncio.Task[None] | None = None

    async def __call__(self, text: str, phase: str) -> None:
        """The words so far, from the model as it produces them."""
        now = time.monotonic()
        if now - self._last >= SAYING_INTERVAL:
            self._last = now
            await self._put(text, phase)
            return
        # The words come in bursts: what arrives within the interval is written once at
        # its end, so the text never stops short of what the model said.
        self._pending = (text, phase)
        if self._later is None:
            self._later = asyncio.create_task(self._flush())

    async def at(self, phase: str) -> None:
        """What the step is at now; its words stay as they are."""
        await self._settle()
        await self._put(self._text, phase)

    async def end(self) -> None:
        """The step is over and what it said is on record."""
        await self._settle()
        await self._put(None, None)

    async def _settle(self) -> None:
        """Nothing held back may land after what comes next."""
        pending, self._pending = self._pending, None
        if self._later is not None:
            self._later.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._later
            self._later = None
        if pending is not None:
            self._text = pending[0]

    async def _flush(self) -> None:
        await asyncio.sleep(SAYING_INTERVAL)
        pending, self._pending, self._later = self._pending, None, None
        if pending is not None:
            self._last = time.monotonic()
            await self._put(*pending)

    async def _put(self, text: str | None, phase: str | None) -> None:
        self._text, self._phase = text, phase
        try:
            async with self._db.tenant(self._tenant_id) as conn:
                await runs.set_saying(conn, self._run_id, self._epoch, text, phase)
        except Exception:
            log.debug("could not write what the agent is saying", exc_info=True)
