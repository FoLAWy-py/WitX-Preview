"""Connection to the cloud: one WebSocket for commands, HTTP for screenshot uploads.

The Gateway always dials out, so phones can sit behind NAT without open ports.
It reconnects with backoff; on every connect it resends unacknowledged results.
It signs in with its machine credential and rotates it when the cloud says it is due.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import websockets
from pydantic import ValidationError
from witx_contracts import gateway as wire
from witx_contracts import logs

from witx_gateway import __version__
from witx_gateway.credentials import CredentialStore
from witx_gateway.devices.base import Device
from witx_gateway.executor import Executor
from witx_gateway.journal import Journal

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 20
SCAN_SECONDS = 10  # how often the Gateway looks for plugged-in or unplugged devices


class MissingCredential(Exception):
    """The Gateway has not enrolled yet."""


LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def checked_cloud_url(url: str, *, allow_insecure: bool = False) -> str:
    """The cloud's address. Anything but this machine must be https: the credential,
    the screenshots and the control channel would otherwise cross the network in the
    clear. ``allow_insecure`` (gateway.toml) is for a trusted local network only."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("cloud_url must be an http(s) address")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK and not allow_insecure:
        raise ValueError(
            f"cloud_url {url} is not encrypted: use https://, or set allow_insecure_cloud "
            "= true in gateway.toml for a trusted local network"
        )
    return url.strip().rstrip("/")


def websocket_url(cloud_url: str) -> str:
    base = cloud_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/gateway/v1/connect"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/gateway/v1/connect"
    raise ValueError("cloud_url must start with http:// or https://")


class GatewayClient:
    def __init__(
        self,
        *,
        cloud_url: str,
        credentials: CredentialStore,
        journal: Journal,
        devices: list[Device],
        http: httpx.AsyncClient | None = None,
        scan: Callable[[], Awaitable[list[wire.DeviceInfo]]] | None = None,
        rescan: asyncio.Event | None = None,
    ) -> None:
        self.cloud_url = cloud_url.rstrip("/")
        self.credentials = credentials
        token = credentials.load()
        if token is None:
            raise MissingCredential
        self.token = token
        self.journal = journal
        self.devices = devices
        # Looks for devices again and says what each needs (discovery.Inventory.refresh); it
        # may add newly found devices to ``devices``, the list given here.
        self._scan = scan
        # Set when something the report shows changed between scans (discovery.Inventory
        # .changed): the watcher looks again at once instead of at the next scan.
        self._rescan = rescan
        self._reported: list[wire.DeviceInfo] | None = None
        self.http = http or httpx.AsyncClient(timeout=60)
        self._ws: Any = None
        self.executor = Executor(journal, self.devices, self._send, self._upload)
        self.connected = asyncio.Event()
        self._welcomed = False  # whether the current attempt got as far as the welcome
        self._work: set[asyncio.Task[None]] = set()  # device work under way, across connections

    async def _send(self, raw: str) -> None:
        ws = self._ws
        if ws is None:
            return  # stays in the outbox; resent after reconnect
        with contextlib.suppress(websockets.ConnectionClosed):
            await ws.send(raw)

    async def _upload(self, observation_id: UUID, data: bytes) -> None:
        response = await self.http.put(
            f"{self.cloud_url}/gateway/v1/observations/{observation_id}/screenshot",
            content=data,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "image/png"},
        )
        response.raise_for_status()

    async def _rotate(self) -> bool:
        """Asks for the next credential and stores it before using it. If either step
        fails the current credential stays valid, so nothing is lost."""
        try:
            response = await self.http.post(
                f"{self.cloud_url}/gateway/v1/credential",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            credential = wire.Credential.model_validate_json(response.content).credential
            self.credentials.save(credential)
        except (httpx.HTTPError, ValidationError, OSError) as error:
            log.warning("could not rotate the credential (%s); keeping the current one", error)
            return False
        self.token = credential
        log.info("credential rotated")
        return True

    async def _inventory(self) -> list[wire.DeviceInfo]:
        if self._scan is not None:
            infos = await self._scan()
            self.executor.know(self.devices)
            return infos
        return [
            wire.DeviceInfo(
                native_id=d.native_id,
                platform=d.platform,
                name=d.name,
                sensitive_apps=d.sensitive_apps,
                capabilities=d.capabilities,
            )
            for d in self.devices
        ]

    async def _watch_devices(self, ws: Any) -> None:
        """Reports the devices again whenever one is plugged in, unplugged or changes."""
        while True:
            await self._next_scan()
            current = await self._inventory()
            if current != self._reported:
                self._reported = current
                await ws.send(wire.Devices(devices=current).model_dump_json())

    async def _next_scan(self) -> None:
        if self._rescan is None:
            await asyncio.sleep(SCAN_SECONDS)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._rescan.wait(), SCAN_SECONDS)
        self._rescan.clear()

    async def run(self, stop: asyncio.Event) -> None:
        self.executor.recover()
        delay = 1.0
        while not stop.is_set():
            self._welcomed = False
            failure: str | None = None
            try:
                await self._session(stop)
            except (websockets.InvalidStatus, websockets.ConnectionClosed) as error:
                refused = (
                    error.response.status_code == 403
                    if isinstance(error, websockets.InvalidStatus)
                    else error.rcvd is not None and error.rcvd.code == 4401
                )
                if refused:
                    log.error("the cloud refused this Gateway: revoked, disabled or not enrolled")
                else:
                    failure = f"connection to cloud lost: {error}"
            except (OSError, websockets.WebSocketException, TimeoutError) as error:
                failure = f"connection to cloud failed: {error}"
            if self._welcomed:
                delay = 1.0  # it had been connected: a new drop starts the backoff over
            if failure:
                log.warning("%s; retrying in %.0fs", failure, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            delay = min(delay * 2, 60)
        for task in self._work:  # shutting down: what is left is settled on restart
            task.cancel()

    async def _session(self, stop: asyncio.Event) -> None:
        async with websockets.connect(
            websocket_url(self.cloud_url),
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=1024 * 1024,
        ) as ws:
            self._reported = await self._inventory()
            await ws.send(
                wire.Hello(gateway_version=__version__, devices=self._reported).model_dump_json()
            )
            welcome = wire.cloud_message.validate_json(await asyncio.wait_for(ws.recv(), 10))
            if not isinstance(welcome, wire.Welcome):
                raise websockets.WebSocketException("expected welcome")
            logs.bind(gateway_id=welcome.gateway_id)
            self._welcomed = True
            self.executor.bind(welcome.devices)
            if welcome.rotate_credential and await self._rotate():
                return  # reconnect with the new credential, which retires the previous one
            self._ws = ws
            self.connected.set()
            log.info(
                "connected as gateway %s with %d devices", welcome.gateway_id, len(welcome.devices)
            )
            for raw in self.journal.outbox():
                await ws.send(raw)
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            watcher = asyncio.create_task(self._watch_devices(ws))
            stopper = asyncio.create_task(stop.wait())
            try:
                while True:
                    receive = asyncio.create_task(ws.recv())
                    done, _ = await asyncio.wait(
                        {receive, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stopper in done:
                        receive.cancel()
                        return
                    self._dispatch(str(receive.result()))
            finally:
                # Work on the devices is not tied to the connection: an input or screenshot
                # under way finishes, and its result waits in the outbox for the next one.
                self._ws = None
                self.connected.clear()
                heartbeat.cancel()
                watcher.cancel()
                stopper.cancel()

    def _dispatch(self, raw: str) -> None:
        try:
            message = wire.cloud_message.validate_json(raw)
        except ValidationError:
            log.warning("ignoring an invalid message from the cloud")
            return
        if isinstance(message, wire.Lease):
            self.executor.lease(message)
            return
        if isinstance(message, wire.Bindings):
            self.executor.bind(message.devices)
            return
        if isinstance(message, wire.ResultAck):
            self.journal.acknowledge(message.ref_id)
            return
        if isinstance(message, wire.Observe):
            work = self.executor.observe(message)
        elif isinstance(message, wire.Action):
            work = self.executor.action(message)
        elif isinstance(message, wire.Stop):
            self.executor.barrier(message)
            work = self.executor.confirm_stop(message)
        elif isinstance(message, wire.FrameRequest):
            work = self.executor.frame(message)
        elif isinstance(message, wire.ManualInput):
            work = self.executor.manual(message)
        else:
            return
        with logs.log_context(**logs.ids(message)):
            task = asyncio.create_task(work)  # copies the context: its logs carry the IDs
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    @staticmethod
    async def _heartbeat(ws: Any) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await ws.send(wire.Heartbeat().model_dump_json())
