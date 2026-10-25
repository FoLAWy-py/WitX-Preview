"""A complete cloud (hub + harness + worker) with a scripted model and a simulated Gateway.

The simulated Gateway answers through the same hub entry points a real one uses, so
these tests exercise the real admission, delivery and result paths end to end.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from witx.config import Limits, parse_models
from witx.domain.model import PermissionMode, Task
from witx.harness.loop import Harness
from witx.harness.plugins import Installed
from witx.hub import Hub, Session
from witx.models import ModelRequest
from witx.objects import LocalObjectStore, screenshot_key
from witx.services import lifecycle
from witx.store import execution, runs, tasks
from witx.store.db import Database, fetch
from witx.worker import Worker
from witx_contracts import gateway as wire

from tests.cloud.support import FakeRisk, ScriptedModel, Tenant, now, png, sha256

SCREEN = {
    "width": 1080,
    "height": 2400,
    "input_width": 1080,
    "input_height": 2400,
    "rotation": 0,
    "foreground_app": "com.android.settings",
}


@dataclass
class SimulatedGateway:
    """Behaves like a Gateway: tracks epochs, captures, executes and acknowledges."""

    outcome: str = "returned"  # what the next action reports
    hold_actions: bool = False  # keep actions in flight (never answer)
    epochs: dict[UUID, int] = field(default_factory=dict)
    actions: list[wire.Action] = field(default_factory=list)
    stops: list[wire.Stop] = field(default_factory=list)
    held: list[wire.Action] = field(default_factory=list)
    manual: list[wire.ManualInput] = field(default_factory=list)  # a person's input
    # What find_app, or launch_app by query, finds; the latter opens the first.
    found_apps: list[dict[str, Any]] = field(default_factory=list)
    field_text: str | None = None  # what the field holds after type_text (None: unread)
    field_name: str | None = None  # ... and what the app calls it
    wrong_field: bool = False  # the focused field is not where the agent pointed
    select_problem: str | None = None  # select_option tapped nothing: "not_found", "ambiguous"
    type_problem: str | None = None  # type_text stopped short: "no_candidate" ...
    field_read: bool = True  # False: the field could not be read back after type_text
    capture_error: str | None = None  # screenshots fail with this, if set
    sliders: list[dict[str, Any]] = field(default_factory=list)  # sliders in view
    same_picture: bool = False  # every screenshot is the same picture, as on a static screen
    shots: int = 0  # screenshots taken; each is another shade of grey unless same_picture


class Rig:
    def __init__(
        self,
        db: Database,
        tenant: Tenant,
        root: Path,
        limits: Limits | None = None,
        catalog: str = "",
        judge: Any = None,
        compressor: Any = None,
        plugins: Installed | None = None,
    ):
        """``catalog`` is a WITX_MODELS value whose first model is the default; the
        harness falls back only among its models."""
        self.db = db
        self.tenant = tenant
        self.objects = LocalObjectStore(root)
        self.limits = limits or Limits()
        self.hub = Hub(db, self.objects, self.limits)
        self.model = ScriptedModel([])
        self.risk = FakeRisk()
        self.worker = Worker(
            db,
            Harness(
                db,
                self.model,  # type: ignore[arg-type]
                self.objects,
                self.limits,
                self.risk,  # type: ignore[arg-type]
                catalog=parse_models(catalog),
                default_model=catalog.split("=")[0] if catalog else None,
                judge=judge,
                compressor=compressor,
                plugins=plugins,
            ),
        )
        self.gateway = SimulatedGateway()
        self.sent: list[str] = []
        self.session = Session(tenant.gateway_id, tenant.id, self._send)

    async def _send(self, raw: str) -> None:
        self.sent.append(raw)
        message = wire.cloud_message.validate_json(raw)
        # Live takeover requests are answered at once, like a connected Gateway would.
        if isinstance(message, (wire.FrameRequest, wire.ManualInput)):
            asyncio.get_running_loop().create_task(self._answer_live(message))

    async def _answer_live(self, message: wire.FrameRequest | wire.ManualInput) -> None:
        g = self.gateway
        if isinstance(message, wire.FrameRequest):
            reply: wire.GatewayMessage = wire.Frame(
                request_id=message.request_id, screen=wire.ScreenState(**SCREEN), jpeg="/9j/"
            )
        else:
            g.manual.append(message)
            stopped = max((s.epoch for s in g.stops if s.device_id == message.device_id), default=0)
            current = message.epoch == g.epochs.get(message.device_id) and message.epoch > stopped
            reply = wire.ManualInputResult(
                input_id=message.input_id,
                outcome="returned" if current else "rejected",
                reason=None if current else "stale_epoch",
            )
        await self.handle(reply)

    async def connect(self, devices: list[dict[str, Any]]) -> None:
        hello = wire.Hello(gateway_version="test", devices=[wire.DeviceInfo(**d) for d in devices])
        await self.hub.hello(self.session, hello)

    # ------------------------------------------------------------ driving

    async def create_task(self, goal: str = "Open Wi-Fi settings", **kw: Any) -> Task:
        async with self.db.tenant(self.tenant.id) as conn:
            return await lifecycle.create_task(
                conn,
                tenant_id=self.tenant.id,
                user_id=self.tenant.user_id,
                request=goal,
                constraints=kw.get("constraints", []),
                device_ids=kw.get("device_ids", self.tenant.device_ids),
                model="test-model",
                permission_mode=kw.get("permission_mode", PermissionMode.AUTO),
                timezone=kw.get("timezone", "UTC"),
            )

    async def settle(self, rounds: int = 20) -> None:
        """Alternates worker steps and Gateway exchanges until nothing moves."""
        for _ in range(rounds):
            moved = await self.worker.run_once()
            moved += await self.gateway_round()
            if not moved:
                return

    async def gateway_round(self) -> int:
        messages = await self.hub.collect(self.session)
        for message in messages:
            await self._answer(message)
        return len(messages)

    async def _answer(self, message: wire.CloudMessage) -> None:
        g = self.gateway
        if isinstance(message, wire.Lease):
            g.epochs[message.device_id] = message.epoch
        elif isinstance(message, wire.Observe):
            if g.capture_error:
                await self.handle(
                    wire.ObservationResult(
                        observation_id=message.observation_id, error=g.capture_error
                    )
                )
            else:
                await self.capture(message.observation_id)
        elif isinstance(message, wire.Action):
            g.actions.append(message)
            if g.hold_actions:
                g.held.append(message)
                return
            await self.finish_action(message, g.outcome)
        elif isinstance(message, wire.Stop):
            g.stops.append(message)
            if not g.held:
                await self.handle(
                    wire.StopAck(stop_id=message.stop_id, device_id=message.device_id)
                )

    async def capture(self, observation_id: UUID, screen: dict[str, Any] | None = None) -> None:
        screen = screen or SCREEN | {"sliders": self.gateway.sliders}
        g = self.gateway
        g.shots += 1
        data = png(screen["width"], screen["height"], 0x80 if g.same_picture else g.shots % 256)
        await self.objects.put(screenshot_key(self.tenant.id, observation_id), data, "image/png")
        await self.handle(
            wire.ObservationResult(
                observation_id=observation_id,
                captured_at=now(),
                screen=wire.ScreenState(**screen),
                sha256=sha256(data),
            )
        )

    async def finish_action(self, action: wire.Action, outcome: str) -> None:
        if action.epoch != self.gateway.epochs.get(action.device_id):
            outcome = "rejected"
        by_query = action.input.kind == "launch_app" and action.input.query is not None
        if by_query and outcome == "returned" and not self.gateway.found_apps:
            outcome = "app_not_found"  # nothing to open: rejected below, with this reason
        search = (action.input.kind == "find_app" or by_query) and outcome == "returned"
        reason = "epoch" if outcome == "rejected" else None
        if outcome == "app_not_found":
            outcome, reason = "rejected", "app_not_found"
        if action.input.kind == "select_option" and outcome == "returned":
            reason = self.gateway.select_problem
        if action.input.kind == "type_text" and outcome == "returned":
            reason = self.gateway.type_problem
        read = action.input.kind != "type_text" or self.gateway.field_read
        if self.gateway.wrong_field and action.input.kind == "type_text" and outcome == "returned":
            outcome, reason = "rejected", "wrong_field"
        await self.handle(
            wire.ActionResult(
                action_id=action.action_id,
                outcome=outcome,  # type: ignore[arg-type]
                reason=reason,
                finished_at=now(),
                apps=[wire.FoundApp(**a) for a in self.gateway.found_apps] if search else None,
                field_text=self.gateway.field_text
                if action.input.kind in ("type_text", "select_option")
                and not (action.input.kind == "select_option" and self.gateway.select_problem)
                and read
                else None,
                field_name=self.gateway.field_name
                if action.input.kind == "type_text" and read
                else None,
                field_box=[144, 2125, 992, 192]
                if action.input.kind == "type_text" and read
                else None,
            )
        )
        if outcome != "rejected":
            await self.capture(action.post_observation_id)

    async def handle(self, message: wire.GatewayMessage) -> None:
        await self.hub.handle(self.session, message.model_dump_json())

    # ------------------------------------------------------------ inspection

    async def task(self, task_id: UUID) -> Task:
        async with self.db.tenant(self.tenant.id) as conn:
            task = await tasks.get(conn, task_id)
        assert task is not None
        return task

    async def root_run(self, task_id: UUID) -> runs.Run:
        async with self.db.tenant(self.tenant.id) as conn:
            return (await runs.for_task(conn, task_id))[0]

    async def actions(self, task_id: UUID) -> list[execution.Action]:
        async with self.db.tenant(self.tenant.id) as conn:
            return await execution.task_actions(conn, task_id)

    async def outputs(self, task_id: UUID) -> list[dict[str, Any]]:
        """Tool results in order, as the model received them."""
        async with self.db.tenant(self.tenant.id) as conn:
            result = []
            for run in await runs.for_task(conn, task_id):
                result += [
                    {"name": c.name, **(c.output or {})}
                    for c in await runs.calls_for_run(conn, run.id)
                ]
            return result

    async def usage(self, task_id: UUID) -> dict[str, Any]:
        async with self.db.tenant(self.tenant.id) as conn:
            return dict(await runs.task_usage(conn, task_id))

    async def risk_calls(self, task_id: UUID) -> list[dict[str, Any]]:
        """The risk checks kept with the task's steps, in order."""
        async with self.db.tenant(self.tenant.id) as conn:
            rows = await fetch(
                conn,
                """SELECT s.risk_call FROM steps s JOIN runs r ON r.id = s.run_id
                    WHERE r.task_id = :t AND s.risk_call IS NOT NULL ORDER BY s.seq""",
                t=task_id,
            )
            return [r["risk_call"] for r in rows]

    async def event_types(self, task_id: UUID) -> list[str]:
        async with self.db.tenant(self.tenant.id) as conn:
            return [r["type"] for r in await tasks.events_after(conn, task_id, 0)]


def last_screenshot(request: ModelRequest) -> str:
    """The newest observation ID the model has been shown."""
    found = None
    for item in request.input:
        for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
            match = re.match(
                r"Screenshot ([0-9a-f-]{36}) of device .*, taken as you start",
                str(part.get("text", "")),
            )
            if match:  # taken as the run started
                found = match.group(1)
        if item.get("type") == "function_call_output":
            output = json.loads(item["output"])
            for candidate in (output, output.get("observation") or {}):
                if candidate.get("status") == "ok" and "observation_id" in candidate:
                    found = candidate["observation_id"]
    assert found, "the model has not seen a screenshot yet"
    return str(found)
