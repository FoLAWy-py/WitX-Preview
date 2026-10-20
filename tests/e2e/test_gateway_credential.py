"""A Gateway rotates its credential over real sockets when the cloud says it is due."""

import asyncio
from pathlib import Path

import uvicorn
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator
from witx.config import Settings
from witx.domain import rules
from witx.hub import Hub
from witx.objects import LocalObjectStore
from witx.services import identity
from witx.store.db import Database, execute
from witx_gateway.client import GatewayClient
from witx_gateway.devices.fake import FakeDevice
from witx_gateway.journal import Journal

from tests.cloud.support import provision
from tests.conftest import Databases
from tests.e2e.test_end_to_end import free_port
from tests.gateway.keystore import stored


async def test_a_gateway_due_for_rotation_stores_its_next_credential_then_reconnects_with_it(
    databases: Databases, db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme")
    async with admin_db.system() as conn:
        await execute(
            conn,
            "UPDATE gateways SET enrolled_at = now() - :age WHERE id = :g",
            age=rules.CREDENTIAL_ROTATE_AFTER,
            g=tenant.gateway_id,
        )
    settings = Settings(database_url=databases.app_url, object_store_url=f"file://{tmp_path}")
    objects = LocalObjectStore(tmp_path)
    app = create_app(
        settings,
        db=db,
        objects=objects,
        authenticator=DevAuthenticator({}),
        hub=Hub(db, objects, settings.limits),
    )
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    credentials = stored(tmp_path / "journal.sqlite3", tenant.gateway_token)
    gateway = GatewayClient(
        cloud_url=f"http://127.0.0.1:{port}",
        credentials=credentials,
        journal=Journal(tmp_path / "journal.sqlite3"),
        devices=[FakeDevice(native_id="emulator-5554")],
    )
    stop = asyncio.Event()
    running = asyncio.create_task(gateway.run(stop))
    try:
        await asyncio.wait_for(gateway.connected.wait(), 5)
        following = credentials.load()
        assert following is not None and following != tenant.gateway_token
        assert gateway.token == following
        # It reconnected with the new credential, which retired the previous one.
        async with db.system() as conn:
            assert await identity.gateway_for_token(conn, tenant.gateway_token) is None
            assert await identity.gateway_for_token(conn, following) is not None
    finally:
        stop.set()
        await running
        server.should_exit = True
        await serving
