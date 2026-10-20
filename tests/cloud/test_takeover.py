"""A person taking over a device of a paused task ."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from witx.api import takeover as socket
from witx.domain import rules
from witx.domain.model import Lease, Task, TaskStatus
from witx.domain.rules import Rejected
from witx.services import lifecycle, takeover
from witx.services.identity import Actor
from witx.store import devices
from witx.store.db import Database, execute, fetch
from witx_contracts import gateway as wire

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def paused(rig: Rig) -> Task:
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)]]
    task = await rig.create_task()
    await rig.worker.run_once()
    await rig.gateway_round()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.pause(conn, task.id)
    await rig.gateway_round()  # barrier confirmed
    return await rig.task(task.id)


async def start(rig: Rig, task: Task) -> Lease:
    async with rig.db.tenant(rig.tenant.id) as conn:
        lease = await takeover.start(
            conn,
            task_id=task.id,
            device_id=rig.tenant.device_ids[0],
            user_id=rig.tenant.user_id,
            now=datetime.now(UTC),
        )
    await rig.gateway_round()  # the Gateway learns the takeover's epoch
    return lease


async def not_responding(rig: Rig) -> None:
    """The Gateway reports that the phone stopped answering."""
    info = device_info(0) | {"problems": ["not_responding"]}
    await rig.handle(wire.Devices(devices=[wire.DeviceInfo(**info)]))


def frame_requests(rig: Rig) -> list[wire.FrameRequest]:
    return [
        m
        for m in map(wire.cloud_message.validate_json, rig.sent)
        if isinstance(m, wire.FrameRequest)
    ]


async def lease_of(rig: Rig) -> Lease | None:
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await devices.lease(conn, rig.tenant.device_ids[0])


class Browser:
    """The browser end of a takeover socket."""

    def __init__(self, rig: Rig, takeover_id: UUID) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.received: list[dict[str, Any]] = []
        self.seq = 0
        actor = Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "owner")
        self.session = asyncio.create_task(
            socket.serve(
                rig.db,
                rig.hub,
                takeover_id=takeover_id,
                who=actor,
                receive=self.inbox.get,
                send=self._receive,
            )
        )

    async def _receive(self, raw: str) -> None:
        self.received.append(json.loads(raw))

    async def next(self, kind: str) -> dict[str, Any]:
        for _ in range(200):
            for message in self.received:
                if message["type"] == kind:
                    self.received.remove(message)
                    return message
            await asyncio.sleep(0.01)
        raise AssertionError(f"no {kind} message")

    async def input(self, frame_id: int, **command: Any) -> dict[str, Any]:
        self.seq += 1
        input_id = str(uuid4())
        message = {"type": "input", "input_id": input_id, "client_seq": self.seq}
        await self.inbox.put(json.dumps(message | {"frame_id": frame_id, "input": command}))
        result = await self.next("input_result")
        assert result["input_id"] == input_id
        return result

    async def end(self, resume: bool = False) -> dict[str, Any]:
        await self.inbox.put(json.dumps({"type": "end", "resume": resume}))
        ended = await self.next("ended")
        await self.session
        return ended


async def test_only_a_paused_task_can_be_taken_over(rig: Rig) -> None:
    task = await rig.create_task()
    with pytest.raises(Rejected) as error:
        await start(rig, task)
    assert error.value.code == "task_not_paused"


async def test_the_persons_input_runs_under_a_new_epoch_of_their_own(rig: Rig) -> None:
    task = await paused(rig)
    paused_epoch = (await lease_of(rig)).epoch  # type: ignore[union-attr]
    lease = await start(rig, task)
    assert lease.epoch > paused_epoch and lease.takeover_user == rig.tenant.user_id

    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = await browser.next("frame")
    result = await browser.input(frame["frame_id"], kind="tap", x=100, y=200)
    assert result["outcome"] == "returned"
    (sent,) = rig.gateway.manual
    assert sent.epoch == lease.epoch and sent.screen.width == frame["width"]
    await browser.end()


async def test_a_takeover_asks_the_gateway_for_pictures_from_the_phones_screen_broadcast(
    rig: Rig,
) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    await browser.next("frame")
    await browser.end()
    requests = [
        m
        for m in map(wire.cloud_message.validate_json, rig.sent)
        if isinstance(m, wire.FrameRequest)
    ]
    assert requests and all(r.stream and not r.if_idle for r in requests)


async def test_a_takeover_asks_for_the_next_picture_before_the_last_one_has_arrived(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    release = asyncio.Event()
    answer = rig._answer_live

    async def held(message: wire.FrameRequest | wire.ManualInput) -> None:
        await release.wait()
        await answer(message)

    monkeypatch.setattr(rig, "_answer_live", held)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    await asyncio.sleep(0.2)
    asked = [
        m
        for m in map(wire.cloud_message.validate_json, rig.sent)
        if isinstance(m, wire.FrameRequest)
    ]
    assert len(asked) == socket.FRAME_WINDOW  # none answered yet, the window is full
    release.set()
    await browser.next("frame")
    await browser.end()


async def test_pictures_reach_the_browser_in_the_order_they_were_asked_for(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    ids = [(await browser.next("frame"))["frame_id"] for _ in range(6)]
    await browser.end()
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


async def test_the_first_picture_may_wait_for_the_phones_session_to_open(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    timeouts: list[float] = []
    ask = rig.hub.ask

    async def spy(gateway_id: UUID, message: Any, timeout: float) -> Any:
        timeouts.append(timeout)
        return await ask(gateway_id, message, timeout)

    monkeypatch.setattr(rig.hub, "ask", spy)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    for _ in range(4):
        await browser.next("frame")
    await browser.end()
    assert timeouts[0] == socket.FIRST_FRAME_TIMEOUT > socket.FRAME_TIMEOUT
    assert socket.FRAME_TIMEOUT in timeouts  # later ones are not left waiting that long


def drag(**overrides: Any) -> dict[str, Any]:
    """A person's drag with the path their pointer took."""
    return {
        "kind": "swipe",
        "x1": 100,
        "y1": 300,
        "x2": 300,
        "y2": 300,
        "duration_ms": 400,
        "path": [
            {"x": 100, "y": 300, "t_ms": 0},
            {"x": 180, "y": 320, "t_ms": 120},
            {"x": 260, "y": 290, "t_ms": 280},
            {"x": 300, "y": 300, "t_ms": 400},
        ],
    } | overrides


async def test_a_drag_reaches_the_gateway_with_the_path_the_person_took(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = await browser.next("frame")
    result = await browser.input(frame["frame_id"], **drag())
    assert result["outcome"] == "returned"
    (sent,) = rig.gateway.manual
    assert sent.input.kind == "swipe" and sent.input.path is not None
    assert [(p.x, p.y, p.t_ms) for p in sent.input.path] == [
        (100, 300, 0),
        (180, 320, 120),
        (260, 290, 280),
        (300, 300, 400),
    ]
    await browser.end()


async def test_a_drag_that_leaves_the_frame_or_is_out_of_order_is_refused_not_the_takeover(
    rig: Rig,
) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = await browser.next("frame")
    off = drag()
    off["path"][1] = {"x": frame["width"], "y": 320, "t_ms": 120}  # one pixel past the frame
    assert (await browser.input(frame["frame_id"], **off))["reason"] == "out_of_bounds"
    backwards = drag()
    backwards["path"][1], backwards["path"][2] = backwards["path"][2], backwards["path"][1]
    assert (await browser.input(frame["frame_id"], **backwards))["reason"] == "invalid_input"
    assert rig.gateway.manual == []  # neither reached the phone
    tap = await browser.input(frame["frame_id"], kind="tap", x=100, y=300)
    assert tap["outcome"] == "returned"  # the takeover goes on
    await browser.end()


async def test_typed_text_is_never_stored(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = await browser.next("frame")
    await browser.input(frame["frame_id"], kind="type_text", text="hunter2-secret")
    await browser.end()
    assert rig.gateway.manual[0].input.text == "hunter2-secret"  # type: ignore[union-attr]
    async with rig.db.tenant(rig.tenant.id) as conn:
        for table in ("events", "task_inputs", "steps", "tool_calls", "actions"):
            rows = await fetch(conn, f"SELECT * FROM {table}")
            assert "hunter2-secret" not in str([dict(r) for r in rows]), table


async def test_ending_refuses_late_input_and_tells_the_agent_to_look_again(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    await browser.next("frame")
    assert (await browser.end())["reason"] == "ended"
    await rig.gateway_round()

    after = await lease_of(rig)
    assert after is not None and after.epoch > lease.epoch and after.takeover_id is None
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED  # still paused
    async with rig.db.tenant(rig.tenant.id) as conn:
        notes = [r["text"] for r in await fetch(conn, "SELECT text FROM task_inputs")]
    assert "take a new screenshot" in notes[-1]
    # The Gateway now runs only the new epoch, so a late input of the takeover (with
    # the takeover's epoch) is refused there (see the Gateway's executor tests).
    assert rig.gateway.epochs[rig.tenant.device_ids[0]] == after.epoch


async def test_a_dropped_connection_ends_the_takeover_and_keeps_the_task_paused(
    rig: Rig,
) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    await browser.next("frame")
    browser.session.cancel()  # stands in for the socket closing mid-takeover
    with pytest.raises(asyncio.CancelledError):
        await browser.session
    after = await lease_of(rig)
    assert after is not None and after.takeover_id is None
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED


async def test_the_takeover_ends_after_idle_time(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "TAKEOVER_IDLE", timedelta(seconds=0.3))
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    assert (await browser.next("ended"))["reason"] == "timeout"
    assert (await lease_of(rig)).takeover_id is None  # type: ignore[union-attr]
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED


@pytest.mark.parametrize("gateway", ["answers_without_a_picture", "does_not_answer"])
async def test_a_takeover_that_gets_no_picture_ends_saying_the_phone_does_not_respond(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, gateway: str
) -> None:
    monkeypatch.setattr(socket, "FIRST_FRAME_TIMEOUT", 0.3)
    task = await paused(rig)
    lease = await start(rig, task)

    async def no_picture(message: wire.FrameRequest | wire.ManualInput) -> None:
        if gateway == "answers_without_a_picture":
            assert isinstance(message, wire.FrameRequest)
            await rig.handle(wire.Frame(request_id=message.request_id, error="timeout"))

    monkeypatch.setattr(rig, "_answer_live", no_picture)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    assert (await browser.next("ended"))["reason"] == "device_not_responding"
    await browser.session
    assert (await lease_of(rig)).takeover_id is None  # type: ignore[union-attr]
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED


async def locked(rig: Rig) -> None:
    """The Gateway reports that the phone's screen is locked or dark."""
    info = device_info(0) | {"problems": ["locked"]}
    await rig.handle(wire.Devices(devices=[wire.DeviceInfo(**info)]))


async def test_a_takeover_of_a_phone_that_stays_locked_ends_at_once_saying_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await paused(rig)
    await locked(rig)
    lease = await start(rig, task)  # a locked phone may be taken over: the Gateway wakes it

    async def still_locked(message: wire.FrameRequest | wire.ManualInput) -> None:
        assert isinstance(message, wire.FrameRequest)
        await rig.handle(wire.Frame(request_id=message.request_id, error="locked"))

    monkeypatch.setattr(rig, "_answer_live", still_locked)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    ended = await asyncio.wait_for(browser.next("ended"), socket.FIRST_FRAME_TIMEOUT / 4)
    assert ended["reason"] == "device_locked"  # not "not responding" 45 s later
    await browser.session
    assert (await lease_of(rig)).takeover_id is None  # type: ignore[union-attr]


async def test_a_locked_phone_can_be_taken_over_but_not_used_or_watched(rig: Rig) -> None:
    await locked(rig)
    async with rig.db.tenant(rig.tenant.id) as conn:
        device = await devices.get(conn, rig.tenant.device_ids[0])
    assert device is not None
    rules.require_usable(device, takeover=True)
    with pytest.raises(Rejected) as error:
        rules.require_usable(device)
    assert error.value.code == "device_not_ready"
    lease = await take_from_pool(rig)
    assert lease.takeover_id is not None


async def test_a_phone_that_does_not_respond_cannot_be_taken_over(rig: Rig) -> None:
    task = await paused(rig)
    await not_responding(rig)
    with pytest.raises(Rejected) as error:
        await start(rig, task)
    assert error.value.code == "device_not_ready"
    assert (await lease_of(rig)).takeover_id is None  # type: ignore[union-attr]


async def test_the_task_resumes_only_after_the_takeover_when_asked(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(Rejected) as error:
            await lifecycle.resume(conn, task.id)
    assert error.value.code == "takeover_active"
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    await browser.next("frame")
    await browser.end(resume=True)
    assert (await rig.task(task.id)).status == TaskStatus.ACTIVE


async def test_input_must_be_recent_in_order_and_a_persons_kind(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = (await browser.next("frame"))["frame_id"]
    assert (await browser.input(frame + 50, kind="tap", x=1, y=1))["reason"] == "stale_frame"
    assert (await browser.input(frame, kind="launch_app", app_id="com.x"))[
        "reason"
    ] == "unsupported_input"
    browser.seq -= 2  # resend an older sequence number
    assert (await browser.input(frame, kind="tap", x=1, y=1))["reason"] == "out_of_order"
    assert rig.gateway.manual == []  # none of them reached the device
    await browser.end()


async def test_a_person_presses_the_phones_side_keys(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = (await browser.next("frame"))["frame_id"]
    for kind in ("volume_up", "volume_down", "lock"):
        assert (await browser.input(frame, kind=kind))["outcome"] == "returned"
    assert [m.input.kind for m in rig.gateway.manual] == ["volume_up", "volume_down", "lock"]
    await browser.end()


async def test_the_agent_is_not_told_of_the_side_keys_a_person_presses(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "keys", devices=1)
    rig = Rig(db, tenant, tmp_path)
    keys = ["tap", "swipe", "home", "volume_up", "volume_down", "lock"]
    await rig.connect([device_info(0) | {"capabilities": keys}])
    rig.model.script = [[call("observe", device_id=tenant.device_ids[0])]]
    await rig.create_task()
    await rig.settle(3)
    told = str(rig.model.requests[0].input)
    assert "inputs: tap, swipe, home" in told
    assert "volume" not in told and "lock" not in told


async def test_input_may_name_any_frame_of_the_last_thirty_seconds_however_many_came(
    rig: Rig,
) -> None:
    """A slow drag is sent against the frame on the canvas when the finger was let go, long
    after the one it began on; frames are kept by time, not by count."""
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    first = (await browser.next("frame"))["frame_id"]
    for _ in range(24):
        await browser.next("frame")
    assert socket.KEPT_FRAMES_S == 30.0  # the longest drag (5 s) + hold (15 s) + the way
    assert (await browser.input(first, kind="tap", x=1, y=1))["outcome"] == "returned"
    await browser.end()


async def test_a_frame_older_than_the_kept_time_is_stale_a_younger_one_is_not(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "KEPT_FRAMES_S", 0.5)  # 30 s, scaled down for the test
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    old = (await browser.next("frame"))["frame_id"]
    await asyncio.sleep(0.6)
    assert (await browser.input(old, kind="tap", x=1, y=1))["reason"] == "stale_frame"
    browser.received.clear()
    recent = (await browser.next("frame"))["frame_id"]
    await asyncio.sleep(0.3)
    assert (await browser.input(recent, kind="tap", x=1, y=1))["outcome"] == "returned"
    await browser.end()


async def test_stopping_the_task_ends_the_takeover(rig: Rig) -> None:
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = (await browser.next("frame"))["frame_id"]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.stop(conn, task.id)
    await rig.gateway_round()  # barrier confirmed, device released
    await browser.inbox.put(
        json.dumps(
            {
                "type": "input",
                "input_id": str(uuid4()),
                "client_seq": 1,
                "frame_id": frame,
                "input": {"kind": "tap", "x": 1, "y": 1},
            }
        )
    )
    assert (await browser.next("ended"))["reason"] == "ended_elsewhere"
    assert (await rig.task(task.id)).status == TaskStatus.STOPPED
    assert await lease_of(rig) is None


def owner(rig: Rig) -> Actor:
    return Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "owner")


async def test_a_person_can_watch_the_screen_of_a_device_the_task_holds(rig: Rig) -> None:
    task = await paused(rig)
    frame = await socket.live_frame(
        rig.db, rig.hub, who=owner(rig), task_id=task.id, device_id=rig.tenant.device_ids[0]
    )
    assert (frame.width, frame.height) == (1080, 2400) and frame.jpeg


async def test_a_device_the_task_does_not_hold_cannot_be_watched_through_it(rig: Rig) -> None:
    await paused(rig)
    with pytest.raises(Rejected, match="not_found"):
        await socket.live_frame(
            rig.db, rig.hub, who=owner(rig), task_id=uuid4(), device_id=rig.tenant.device_ids[0]
        )


async def test_watching_gives_way_to_a_takeover(rig: Rig) -> None:
    task = await paused(rig)
    await start(rig, task)
    with pytest.raises(Rejected, match="device_busy"):
        await socket.live_frame(
            rig.db, rig.hub, who=owner(rig), task_id=task.id, device_id=rig.tenant.device_ids[0]
        )


async def test_a_phone_that_does_not_respond_is_not_asked_for_a_picture_to_watch(
    rig: Rig,
) -> None:
    task = await paused(rig)
    await not_responding(rig)
    asked = len(frame_requests(rig))
    with pytest.raises(Rejected, match="device_not_ready"):
        await socket.live_frame(
            rig.db, rig.hub, who=owner(rig), task_id=task.id, device_id=rig.tenant.device_ids[0]
        )
    assert len(frame_requests(rig)) == asked  # the Gateway was not asked


async def take_from_pool(rig: Rig, now: datetime | None = None) -> Lease:
    async with rig.db.tenant(rig.tenant.id) as conn:
        lease = await takeover.start_on_device(
            conn,
            device_id=rig.tenant.device_ids[0],
            user_id=rig.tenant.user_id,
            now=now or datetime.now(UTC),
        )
    await rig.gateway_round()
    return lease


async def test_a_free_device_is_taken_over_from_the_pool_and_free_again_after(rig: Rig) -> None:
    lease = await take_from_pool(rig)
    assert lease.task_id is None and lease.takeover_user == rig.tenant.user_id
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    frame = await browser.next("frame")
    assert (await browser.input(frame["frame_id"], kind="tap", x=10, y=20))["outcome"] == "returned"
    assert rig.gateway.manual[0].epoch == lease.epoch
    await browser.end()
    await rig.gateway_round()

    assert await lease_of(rig) is None
    # The Gateway moved on to a new epoch: late input of the takeover is refused there.
    assert rig.gateway.epochs[rig.tenant.device_ids[0]] > lease.epoch
    async with rig.db.tenant(rig.tenant.id) as conn:
        (change, *_) = await devices.changes(conn, rig.tenant.device_ids[0])
    assert change["change"] == "taken_over"


async def test_a_task_waits_for_a_device_a_person_controls(rig: Rig) -> None:
    lease = await take_from_pool(rig)
    rig.model.script = [[call("observe", device_id=rig.tenant.device_ids[0])]]
    await rig.create_task()
    await rig.worker.run_once()
    assert (await lease_of(rig)).task_id is None  # type: ignore[union-attr]
    assert not rig.model.requests

    async with rig.db.tenant(rig.tenant.id) as conn:
        await takeover.end(conn, takeover_id=lease.takeover_id, reason="ended", inputs=0)  # type: ignore[arg-type]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await execute(conn, "UPDATE runs SET next_wake_at = now()")  # no need to wait
    await rig.worker.run_once()
    assert (await lease_of(rig)).task_id is not None  # type: ignore[union-attr]


async def test_a_pool_takeover_that_ran_out_holds_nothing(rig: Rig) -> None:
    await take_from_pool(rig, now=datetime.now(UTC) - 2 * rules.TAKEOVER_IDLE)
    rig.model.script = [[call("observe", device_id=rig.tenant.device_ids[0])]]
    task = await rig.create_task()
    await rig.worker.run_once()
    assert (await lease_of(rig)).task_id == task.id  # type: ignore[union-attr]


async def test_a_device_a_task_holds_is_taken_over_from_the_task_not_the_pool(rig: Rig) -> None:
    await paused(rig)
    with pytest.raises(Rejected) as error:
        await take_from_pool(rig)
    assert error.value.code == "device_busy"


async def test_a_free_phone_that_does_not_respond_cannot_be_taken_over_from_the_pool(
    rig: Rig,
) -> None:
    await not_responding(rig)
    with pytest.raises(Rejected) as error:
        await take_from_pool(rig)
    assert error.value.code == "device_not_ready"
    assert await lease_of(rig) is None


async def test_only_one_person_controls_a_device_from_the_pool(rig: Rig) -> None:
    await take_from_pool(rig)
    with pytest.raises(Rejected) as error:
        await take_from_pool(rig)
    assert error.value.code == "takeover_active"


async def test_saying_you_are_still_there_keeps_the_takeover_without_any_input(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each "keep" comes well inside the idle time (a slow machine takes a while to handle
    # one), and all of them together last longer than it.
    monkeypatch.setattr(rules, "TAKEOVER_IDLE", timedelta(seconds=1.5))
    task = await paused(rig)
    lease = await start(rig, task)
    browser = Browser(rig, lease.takeover_id)  # type: ignore[arg-type]
    await browser.next("frame")
    for _ in range(8):
        await asyncio.sleep(0.3)
        await browser.inbox.put(json.dumps({"type": "keep"}))
    assert not [m for m in browser.received if m["type"] == "ended"]
    assert rig.gateway.manual == []  # nothing ran on the phone
    assert (await browser.end())["reason"] == "ended"
