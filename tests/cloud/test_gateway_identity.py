"""A Gateway's machine identity : one-time enrollment, rotation, disabling and
revocation."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator
from witx.config import Settings
from witx.domain import rules
from witx.hub import GatewayRemoved, Hub
from witx.objects import LocalObjectStore
from witx.services import identity
from witx.store import devices
from witx.store.db import Database, execute
from witx_contracts import gateway as wire

from tests.cloud.support import Tenant, device_info, provision
from tests.conftest import Databases


@pytest.fixture
async def cloud(
    databases: Databases, db: Database, admin_db: Database, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, Tenant, Hub]]:
    tenant = await provision(admin_db, "acme")
    settings = Settings(database_url=databases.app_url, object_store_url=f"file://{tmp_path}")
    objects = LocalObjectStore(tmp_path)
    hub = Hub(db, objects, settings.limits)
    app = create_app(settings, db=db, objects=objects, authenticator=DevAuthenticator({}), hub=hub)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cloud") as http:
        yield http, tenant, hub


async def new_code(admin_db: Database, tenant_id: UUID, *, now: datetime | None = None) -> str:
    async with admin_db.system() as conn:
        _, code = await identity.create_gateway(
            conn, tenant_id=tenant_id, name="mac-mini", now=now or datetime.now(UTC)
        )
    return code


async def signs_in(db: Database, credential: str) -> tuple[UUID, UUID] | None:
    """What every Gateway request goes through (WebSocket and uploads alike)."""
    async with db.system() as conn:
        return await identity.gateway_for_token(conn, credential)


async def enroll(http: httpx.AsyncClient, code: str) -> httpx.Response:
    return await http.post("/gateway/v1/enroll", json={"code": code, "host": "mac-mini"})


async def test_an_enrollment_code_works_once_and_gives_the_gateway_its_credential(  # type: ignore[no-untyped-def]
    cloud, db: Database, admin_db: Database
) -> None:
    http, tenant, _ = cloud
    code = await new_code(admin_db, tenant.id)
    first = await enroll(http, code)
    assert first.status_code == 200
    credential = wire.Credential.model_validate(first.json())
    assert await signs_in(db, credential.credential) == (credential.gateway_id, tenant.id)
    assert (await enroll(http, code)).status_code == 403


async def test_an_expired_enrollment_code_is_refused(cloud, admin_db: Database) -> None:  # type: ignore[no-untyped-def]
    http, tenant, _ = cloud
    issued = datetime.now(UTC) - rules.ENROLL_CODE_TTL - timedelta(minutes=1)
    assert (await enroll(http, await new_code(admin_db, tenant.id, now=issued))).status_code == 403


async def test_an_enrollment_code_enrolls_only_into_its_own_tenant(  # type: ignore[no-untyped-def]
    cloud, admin_db: Database
) -> None:
    http, tenant, _ = cloud
    other = await provision(admin_db, "globex")
    secret = (await new_code(admin_db, tenant.id)).partition(".")[2]
    assert (await enroll(http, f"{other.id}.{secret}")).status_code == 403
    assert (await enroll(http, "not-a-code")).status_code == 403


async def test_a_rotated_credential_replaces_the_previous_one_when_first_used(  # type: ignore[no-untyped-def]
    cloud, db: Database
) -> None:
    http, tenant, _ = cloud
    previous = tenant.gateway_token
    response = await http.post(
        "/gateway/v1/credential", headers={"Authorization": f"Bearer {previous}"}
    )
    following = wire.Credential.model_validate(response.json()).credential
    # Until the Gateway has stored and used the new one, the previous one still works.
    assert await signs_in(db, previous) is not None
    assert await signs_in(db, following) == (tenant.gateway_id, tenant.id)
    assert await signs_in(db, previous) is None
    refused = await http.post(
        "/gateway/v1/credential", headers={"Authorization": f"Bearer {previous}"}
    )
    assert refused.status_code == 401


async def test_a_disabled_gateway_is_refused_until_enabled_and_a_revoked_one_for_good(  # type: ignore[no-untyped-def]
    cloud, db: Database, admin_db: Database
) -> None:
    _, tenant, _ = cloud
    async with admin_db.system() as conn:
        assert await devices.set_gateway_disabled(conn, tenant.gateway_id, True)
    assert await signs_in(db, tenant.gateway_token) is None
    async with admin_db.system() as conn:
        assert await devices.set_gateway_disabled(conn, tenant.gateway_id, False)
    assert await signs_in(db, tenant.gateway_token) is not None
    async with admin_db.system() as conn:
        assert await devices.revoke_gateway(conn, tenant.gateway_id)
        assert not await devices.set_gateway_disabled(conn, tenant.gateway_id, False)
    assert await signs_in(db, tenant.gateway_token) is None


class Connection:
    """One Gateway connection to the hub, driven by the test."""

    def __init__(self, hub: Hub, tenant: Tenant) -> None:
        self.inbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.outbound: asyncio.Queue[str] = asyncio.Queue()
        self.serving = asyncio.create_task(
            hub.serve(tenant.gateway_id, tenant.id, self.receive, self.outbound.put)
        )

    async def receive(self) -> str:
        raw = await self.inbound.get()
        if raw is None:
            raise EOFError  # the socket closed
        return raw

    async def close(self) -> None:
        await self.inbound.put(None)
        with contextlib.suppress(EOFError):
            await self.serving

    async def welcome(self) -> wire.Welcome:
        hello = wire.Hello(gateway_version="test", devices=[wire.DeviceInfo(**device_info(0))])
        await self.inbound.put(hello.model_dump_json())
        welcome = wire.cloud_message.validate_json(await asyncio.wait_for(self.outbound.get(), 5))
        assert isinstance(welcome, wire.Welcome)
        return welcome


async def test_a_connected_gateway_is_cut_off_at_its_next_heartbeat_once_revoked(  # type: ignore[no-untyped-def]
    cloud, admin_db: Database
) -> None:
    _, tenant, hub = cloud
    connection = Connection(hub, tenant)
    await connection.welcome()
    await connection.inbound.put(wire.Heartbeat().model_dump_json())
    await asyncio.sleep(0.2)
    assert not connection.serving.done()
    async with admin_db.system() as conn:
        await devices.revoke_gateway(conn, tenant.gateway_id)
    await connection.inbound.put(wire.Heartbeat().model_dump_json())
    with pytest.raises(GatewayRemoved):
        await asyncio.wait_for(connection.serving, 5)


async def test_the_gateway_is_asked_to_rotate_a_credential_30_days_old(  # type: ignore[no-untyped-def]
    cloud, admin_db: Database
) -> None:
    _, tenant, hub = cloud
    fresh = Connection(hub, tenant)
    assert not (await fresh.welcome()).rotate_credential
    await fresh.close()
    async with admin_db.system() as conn:
        await execute(
            conn,
            "UPDATE gateways SET enrolled_at = now() - :age WHERE id = :g",
            age=rules.CREDENTIAL_ROTATE_AFTER,
            g=tenant.gateway_id,
        )
    old = Connection(hub, tenant)
    assert (await old.welcome()).rotate_credential
    await old.close()


async def test_the_devices_of_a_revoked_gateway_leave_the_device_list(
    db: Database, admin_db: Database
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    async with admin_db.system() as conn:
        await devices.set_gateway_disabled(conn, tenant.gateway_id, True)
    async with db.tenant(tenant.id) as conn:
        assert [d.id for d in await devices.list_all(conn)] == tenant.device_ids
    async with admin_db.system() as conn:
        await devices.revoke_gateway(conn, tenant.gateway_id)
    async with db.tenant(tenant.id) as conn:
        assert await devices.list_all(conn) == []
