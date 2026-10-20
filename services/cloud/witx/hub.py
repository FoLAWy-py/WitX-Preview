"""Gateway hub: one session per connected Gateway.

The hub keeps no state of its own beyond what it has already sent on the current
connection. Everything to deliver is read from the database, so a reconnect simply
resends whatever is still open; the Gateway's journal makes repeats harmless.
"""

import asyncio
import contextlib
import hashlib
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from witx_contracts import gateway as wire
from witx_contracts import logs

from witx.config import Limits
from witx.domain import rules
from witx.domain.model import Action, ActionStatus, Device, Observation, ObservationStatus
from witx.domain.rules import Rejected
from witx.objects import ObjectNotFound, ObjectStore
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.store import devices, execution, runs, tasks
from witx.store.db import Database

log = logging.getLogger(__name__)

MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024


def screenshot_key(tenant_id: UUID, observation_id: UUID) -> str:
    return f"{tenant_id}/observations/{observation_id}.png"


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


@dataclass
class Session:
    gateway_id: UUID
    tenant_id: UUID
    send: Callable[[str], Awaitable[None]]
    devices: dict[UUID, Device] = field(default_factory=dict)
    sent_epochs: dict[UUID, int] = field(default_factory=dict)
    sent_stops: set[UUID] = field(default_factory=set)
    resent: bool = False


class GatewayRemoved(Exception):
    """The connected Gateway was revoked or disabled; its connection must close."""


class Hub:
    def __init__(
        self, db: Database, objects: ObjectStore, limits: Limits, *, poll_seconds: float = 0.25
    ) -> None:
        self.db = db
        self.objects = objects
        self.limits = limits
        self.poll_seconds = poll_seconds
        self._sessions: dict[UUID, asyncio.Task[None]] = {}
        # For takeovers: the live connection of each Gateway, and the answers awaited.
        # A takeover therefore needs the browser and the Gateway on the same API process.
        self._live: dict[UUID, Session] = {}
        self._awaited: dict[UUID, asyncio.Future[wire.GatewayMessage]] = {}

    async def serve(
        self,
        gateway_id: UUID,
        tenant_id: UUID,
        receive: Callable[[], Awaitable[str]],
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        """Runs one Gateway connection until it closes. A newer connection from the same
        Gateway replaces this one."""
        previous = self._sessions.get(gateway_id)
        if previous is not None:
            previous.cancel()
        current = asyncio.current_task()
        assert current is not None
        self._sessions[gateway_id] = current
        session = Session(gateway_id, tenant_id, send)
        logs.bind(gateway_id=gateway_id, tenant_id=tenant_id)  # this connection's task only
        log.info("gateway connected")
        try:
            hello = wire.gateway_message.validate_json(await asyncio.wait_for(receive(), 10))
            if not isinstance(hello, wire.Hello):
                return
            await self.hello(session, hello)
            dispatcher = asyncio.create_task(self._dispatch_loop(session))
            try:
                while True:
                    await self.handle(session, await receive())
            finally:
                dispatcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatcher
        finally:
            if self._live.get(gateway_id) is session:
                del self._live[gateway_id]
            if self._sessions.get(gateway_id) is current:
                del self._sessions[gateway_id]
                async with self.db.tenant(tenant_id) as conn:
                    await devices.set_gateway_offline(conn, gateway_id)
            log.info("gateway disconnected")

    async def hello(self, session: Session, hello: wire.Hello) -> None:
        registered = await self._register(session, hello.devices)
        async with self.db.tenant(session.tenant_id) as conn:
            issued_at = await devices.credential_issued_at(conn, session.gateway_id)
        self._live[session.gateway_id] = session
        welcome = wire.Welcome(
            gateway_id=session.gateway_id,
            devices=[wire.DeviceBinding(native_id=d.native_id, device_id=d.id) for d in registered],
            rotate_credential=rules.credential_due(issued_at, datetime.now(UTC)),
        )
        await session.send(welcome.model_dump_json())

    async def _register(self, session: Session, reported: list[wire.DeviceInfo]) -> list[Device]:
        """Keeps what the Gateway reports about its devices, then wakes the runs that wait
        for one of them that can be used now. The wake-up is a transaction of its
        own, after the report is committed: a step that found the device unusable held
        its row until it began to wait, so it is waiting by then; and the two never take
        a run's and a device's locks in opposite order."""
        async with self.db.tenant(session.tenant_id) as conn:
            registered = await devices.sync_gateway_devices(
                conn,
                tenant_id=session.tenant_id,
                gateway_id=session.gateway_id,
                devices=[d.model_dump() for d in reported],
            )
        session.devices = {d.id: d for d in registered}
        async with self.db.tenant(session.tenant_id) as conn:
            for run in await runs.wake_for_devices(
                conn, [d.id for d in registered if rules.usable(d)]
            ):
                # The task page stops saying it waits for the phone.
                await tasks.emit(
                    conn, run.task_id, "run.status", run_id=run.id, status=run.status.value
                )
        return registered

    # ------------------------------------------------------------ cloud -> Gateway

    async def _dispatch_loop(self, session: Session) -> None:
        while True:
            for message in await self.collect(session):
                await session.send(message.model_dump_json())
            await asyncio.sleep(self.poll_seconds)

    async def collect(self, session: Session) -> list[wire.CloudMessage]:
        """Everything to send to this Gateway now, in order. Marks it as sent."""
        now = datetime.now(UTC)
        ids = list(session.devices)
        out: list[wire.CloudMessage] = []
        async with self.db.tenant(session.tenant_id) as conn:
            # The device's epoch, held or not: ending a takeover outside a task frees the
            # device under a new epoch, and the Gateway must refuse that takeover's input.
            for device_id, epoch in (await devices.epochs(conn, ids)).items():
                if epoch and session.sent_epochs.get(device_id) != epoch:  # 0: never held
                    out.append(wire.Lease(device_id=device_id, epoch=epoch))
                    session.sent_epochs[device_id] = epoch
            for lease in await devices.leases(conn, ids):
                if lease.stop_id and lease.stop_id not in session.sent_stops:
                    out.append(
                        wire.Stop(
                            stop_id=lease.stop_id, device_id=lease.device_id, epoch=lease.epoch
                        )
                    )
                    session.sent_stops.add(lease.stop_id)
            if not session.resent:
                # After a (re)connect, repeat what may have been lost with the old socket.
                session.resent = True
                for obs in await execution.unfinished_observations(conn, ids):
                    if not await execution.action_by_post_observation(conn, obs.id):
                        out.append(wire.Observe(observation_id=obs.id, device_id=obs.device_id))
                for action in await execution.actions_with_status(conn, ids, ActionStatus.SENT):
                    assert action.observation_id is not None and action.sent_at is not None
                    observation = await execution.get_observation(conn, action.observation_id)
                    assert observation is not None
                    out.append(self._action(action, observation, action.sent_at))
            for obs in await execution.observations_to_send(conn, ids):
                await execution.mark_observation_sent(conn, obs.id)
                out.append(wire.Observe(observation_id=obs.id, device_id=obs.device_id))
            for action, observation in await exec_service.take_deliverable(
                conn, ids, limits=self.limits, now=now
            ):
                out.append(self._action(action, observation, now))
        return out

    def _action(self, action: Action, observation: Observation, sent_at: datetime) -> wire.Action:
        screen = observation.screen
        assert screen is not None
        return wire.Action(
            action_id=action.id,
            device_id=action.device_id,
            epoch=action.epoch,
            input=wire.action_input.validate_python(action.input),
            expected=wire.ScreenState(**{k: v for k, v in vars(screen).items() if k != "sliders"}),
            digest=action.digest,
            expires_at=sent_at + self.limits.action_ttl,
            post_observation_id=action.post_observation_id,
        )

    # ------------------------------------------------------------ Gateway -> cloud

    def connected_ids(self) -> set[UUID]:
        """The Gateways connected to this API process now."""
        return set(self._live)

    async def ask(
        self, gateway_id: UUID, message: wire.FrameRequest | wire.ManualInput, timeout: float
    ) -> wire.GatewayMessage:
        """Sends a takeover request to a connected Gateway and waits for its answer."""
        session = self._live.get(gateway_id)
        if session is None:
            raise Rejected("device_offline")
        ref = message.request_id if isinstance(message, wire.FrameRequest) else message.input_id
        answer: asyncio.Future[wire.GatewayMessage] = asyncio.get_running_loop().create_future()
        self._awaited[ref] = answer
        try:
            await session.send(message.model_dump_json())
            return await asyncio.wait_for(answer, timeout)
        except TimeoutError:
            raise Rejected("device_timeout") from None
        finally:
            self._awaited.pop(ref, None)

    async def handle(self, session: Session, raw: str) -> None:
        try:
            message = wire.gateway_message.validate_json(raw)
        except ValidationError:
            log.warning("gateway sent an invalid message")
            return
        with logs.log_context(**logs.ids(message)):
            await self._handle(session, message)

    async def _handle(self, session: Session, message: wire.GatewayMessage) -> None:
        if isinstance(message, wire.Heartbeat):
            # A revoked or disabled Gateway is cut off within one heartbeat.
            async with self.db.tenant(session.tenant_id) as conn:
                if not await devices.gateway_allowed(conn, session.gateway_id):
                    raise GatewayRemoved(session.gateway_id)
            return
        if isinstance(message, wire.Devices):
            # Plugged in, unplugged or changed: same registration as on connecting. The
            # bindings sent in the welcome stay valid (a device keeps its ID).
            registered = await self._register(session, message.devices)
            bindings = [
                wire.DeviceBinding(native_id=d.native_id, device_id=d.id) for d in registered
            ]
            await session.send(wire.Bindings(devices=bindings).model_dump_json())
            return
        if isinstance(message, (wire.Frame, wire.ManualInputResult)):
            ref = message.request_id if isinstance(message, wire.Frame) else message.input_id
            answer = self._awaited.get(ref)
            if answer is not None and not answer.done():
                answer.set_result(message)
            return  # live answers are not stored, so not acknowledged
        if isinstance(message, wire.ObservationResult):
            await self._observation_result(session, message)
            ref = message.observation_id
        elif isinstance(message, wire.ActionResult):
            log.info("action result", extra={"outcome": message.outcome, "reason": message.reason})
            async with self.db.tenant(session.tenant_id) as conn:
                action = await execution.get_action(conn, message.action_id)
                if action is not None and action.device_id in session.devices:
                    await exec_service.record_result(
                        conn,
                        action,
                        outcome=message.outcome,
                        reason=message.reason,
                        finished_at=message.finished_at,
                        apps=None
                        if message.apps is None
                        else [a.model_dump() for a in message.apps],
                        field_text=message.field_text,
                        field_name=message.field_name,
                        field_box=message.field_box,
                    )
            ref = message.action_id
        elif isinstance(message, wire.StopAck):
            if message.device_id in session.devices:
                async with self.db.tenant(session.tenant_id) as conn:
                    await lifecycle.stop_acknowledged(
                        conn, device_id=message.device_id, stop_id=message.stop_id
                    )
            ref = message.stop_id
        else:
            return
        await session.send(wire.ResultAck(ref_id=ref).model_dump_json())

    async def _observation_result(self, session: Session, msg: wire.ObservationResult) -> None:
        async with self.db.tenant(session.tenant_id) as conn:
            obs = await execution.get_observation(conn, msg.observation_id)
        if obs is None or obs.device_id not in session.devices:
            return
        if obs.status in (ObservationStatus.COMPLETE, ObservationStatus.FAILED):
            return
        error = msg.error
        key = screenshot_key(session.tenant_id, obs.id)
        if error is None:
            error = await self._verify_upload(key, msg)
        async with self.db.tenant(session.tenant_id) as conn:
            await exec_service.record_observation(
                conn,
                obs,
                captured_at=msg.captured_at,
                screen=msg.screen.model_dump() if msg.screen else None,
                sha256=msg.sha256,
                object_key=key,
                error=error,
            )

    async def _verify_upload(self, key: str, msg: wire.ObservationResult) -> str | None:
        """The screenshot must be uploaded, match the reported hash and size."""
        if msg.screen is None or msg.sha256 is None or msg.captured_at is None:
            return "incomplete_result"
        try:
            data = await self.objects.get(key)
        except ObjectNotFound:
            return "not_uploaded"
        if hashlib.sha256(data).hexdigest() != msg.sha256:
            return "hash_mismatch"
        if png_size(data) != (msg.screen.width, msg.screen.height):
            return "size_mismatch"
        return None
