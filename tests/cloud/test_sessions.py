"""Browser sign-in: session cookies, CSRF, and the backend OIDC flow."""

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn
import websockets
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from witx import cli
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator, OidcLogin
from witx.config import Settings
from witx.domain import rules
from witx.hub import Hub
from witx.objects import LocalObjectStore
from witx.store.db import Database, execute

from tests.cloud.support import Tenant, provision
from tests.conftest import Databases

PUBLIC = "http://web.example"
ISSUER = "https://id.example"
CLIENT = "witx-web"
DEV_TOKEN = "dev-token-acme"


class FakeProvider:
    """An OIDC provider: discovery, keys, and a token endpoint that hands out the ID
    token the test prepared."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)
        self.id_token = ""
        self.token_requests: list[httpx.Request] = []

    def sign(self, **claims: Any) -> str:
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": CLIENT, "iat": now, "exp": now + 300} | claims
        return jwt.encode({"alg": "RS256", "kid": "k1"}, payload, self.key)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json=KeySet([self.key]).as_dict(private=False))
        if path == "/token":
            self.token_requests.append(request)
            return httpx.Response(200, json={"id_token": self.id_token, "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
async def system(
    databases: Databases, db: Database, admin_db: Database, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, Tenant, FakeProvider, Any]]:
    tenant = await provision(admin_db, "acme", devices=1)
    provider = FakeProvider()
    settings = Settings(
        database_url=databases.app_url,
        object_store_url=f"file://{tmp_path}",
        dev_tokens={DEV_TOKEN: tenant.subject},
        public_url=PUBLIC,
    )
    objects = LocalObjectStore(tmp_path)
    app = create_app(
        settings,
        db=db,
        objects=objects,
        authenticator=DevAuthenticator(settings.dev_tokens),
        hub=Hub(db, objects, settings.limits),
        login=OidcLogin(
            issuer=ISSUER,
            client_id=CLIENT,
            client_secret="s3cret",
            redirect_uri=f"{PUBLIC}/api/v1/auth/callback",
            transport=httpx.MockTransport(provider.handler),
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as web:
        yield web, tenant, provider, app


async def dev_session(web: httpx.AsyncClient) -> str:
    """Signs in with the developer token; returns the CSRF token."""
    response = await web.post("/api/v1/auth/dev-login", json={"token": DEV_TOKEN})
    assert response.status_code == 204
    return web.cookies["witx_csrf"]


async def test_dev_sign_in_gives_a_session_cookie_that_signs_requests(system) -> None:  # type: ignore[no-untyped-def]
    web, tenant, *_ = system
    assert (await web.get("/api/v1/me")).status_code == 401
    await dev_session(web)
    me = await web.get("/api/v1/me")  # no Authorization header: the cookie signs it
    assert me.status_code == 200 and me.json()["user_id"] == str(tenant.user_id)


async def test_a_write_with_the_session_needs_its_csrf_token_and_our_origin(system) -> None:  # type: ignore[no-untyped-def]
    web, tenant, *_ = system
    csrf = await dev_session(web)
    body = {"goal": "Open Settings", "device_ids": [str(tenant.device_ids[0])]}
    assert (await web.post("/api/v1/tasks", json=body)).status_code == 403
    wrong = await web.post("/api/v1/tasks", json=body, headers={"X-CSRF-Token": "guess"})
    assert wrong.status_code == 403
    foreign = await web.post(
        "/api/v1/tasks",
        json=body,
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    ours = await web.post(
        "/api/v1/tasks", json=body, headers={"X-CSRF-Token": csrf, "Origin": PUBLIC}
    )
    assert ours.status_code == 201
    assert len((await web.get("/api/v1/tasks")).json()) == 1


async def test_signing_out_ends_the_session(system) -> None:  # type: ignore[no-untyped-def]
    web, *_ = system
    csrf = await dev_session(web)
    token = web.cookies["witx_session"]
    assert (
        await web.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    ).status_code == 204
    replayed = await web.get("/api/v1/me", headers={"Cookie": f"witx_session={token}"})
    assert replayed.status_code == 401


async def test_an_idle_session_expires(system, admin_db: Database) -> None:  # type: ignore[no-untyped-def]
    web, *_ = system
    await dev_session(web)
    idle = rules.SESSION_IDLE + rules.SESSION_IDLE / 10
    async with admin_db.system() as conn:
        await execute(conn, "UPDATE sessions SET seen_at = now() - :idle", idle=idle)
    assert (await web.get("/api/v1/me")).status_code == 401


async def sign_in_with_provider(
    web: httpx.AsyncClient, provider: FakeProvider, **claims: Any
) -> httpx.Response:
    """Runs the redirect dance; the provider issues an ID token with ``claims``."""
    started = await web.get("/api/v1/auth/login", params={"return_to": "/tasks/t1"})
    assert started.status_code == 302
    query = parse_qs(urlsplit(started.headers["location"]).query)
    expected = {"nonce": query["nonce"][0]}
    provider.id_token = provider.sign(**(expected | claims))
    return await web.get(
        "/api/v1/auth/callback",
        params={"code": "c1", "state": claims.pop("state", query["state"][0])},
    )


async def test_oidc_sign_in_creates_a_session_and_returns_to_the_page(system) -> None:  # type: ignore[no-untyped-def]
    web, tenant, provider, _ = system
    started = await web.get("/api/v1/auth/login", params={"return_to": "/tasks/t1"})
    query = parse_qs(urlsplit(started.headers["location"]).query)
    assert query["client_id"] == [CLIENT] and query["code_challenge_method"] == ["S256"]
    provider.id_token = provider.sign(sub=tenant.subject, nonce=query["nonce"][0])
    done = await web.get("/api/v1/auth/callback", params={"code": "c1", "state": query["state"][0]})
    assert done.status_code == 302 and done.headers["location"] == f"{PUBLIC}/tasks/t1"
    assert (await web.get("/api/v1/me")).status_code == 200
    # The backend redeemed the code as a confidential client, with the PKCE verifier.
    (token_request,) = provider.token_requests
    form = parse_qs(token_request.content.decode())
    secret = base64.b64encode(f"{CLIENT}:s3cret".encode()).decode()
    assert token_request.headers["authorization"] == f"Basic {secret}"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query["code_challenge"] == [challenge]


@pytest.mark.parametrize(
    "claims",
    [
        {"state": "forged"},
        {"nonce": "replayed"},
        {"aud": "another-client"},
        {"iss": "https://evil.example"},
    ],
)
async def test_oidc_sign_in_refuses_a_forged_state_or_a_foreign_id_token(  # type: ignore[no-untyped-def]
    system, claims: dict[str, str]
) -> None:
    web, tenant, provider, _ = system
    done = await sign_in_with_provider(web, provider, sub=tenant.subject, **claims)
    assert done.headers["location"] == f"{PUBLIC}/sign-in?error=not_completed"
    assert "witx_session" not in web.cookies


async def test_oidc_sign_in_refuses_people_not_invited(system) -> None:  # type: ignore[no-untyped-def]
    web, _, provider, _ = system
    done = await sign_in_with_provider(web, provider, sub="stranger")
    assert done.headers["location"] == f"{PUBLIC}/sign-in?error=not_invited"
    assert "witx_session" not in web.cookies


def test_after_sign_in_only_a_path_on_this_site_is_followed() -> None:
    assert rules.safe_return_path("/tasks/1?x=2") == "/tasks/1?x=2"
    for elsewhere in ("//evil.example/x", "https://evil.example", "/\\evil.example", None):
        assert rules.safe_return_path(elsewhere) == "/"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_the_takeover_socket_admits_browsers_only_from_our_pages(system) -> None:  # type: ignore[no-untyped-def]
    web, _, _, app = system
    await dev_session(web)
    token = web.cookies["witx_session"]
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    url = f"ws://127.0.0.1:{port}/api/v1/takeovers/{uuid4()}/connect"
    cookie = {"Cookie": f"witx_session={token}"}
    try:
        async with websockets.connect(
            url, origin="https://evil.example", additional_headers=cookie
        ) as ws:  # type: ignore[arg-type]
            with pytest.raises(websockets.ConnectionClosed) as closed:
                await ws.recv()
            assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401
        async with websockets.connect(url, origin=PUBLIC, additional_headers=cookie) as ws:  # type: ignore[arg-type]
            # Signed in: the session is accepted and the (unknown) takeover is reported.
            assert json.loads(await ws.recv()) == {"type": "ended", "reason": "not_active"}
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serving


@pytest.mark.parametrize("auth", [None, "OIDC"])
def test_the_api_does_not_start_unless_how_people_sign_in_is_chosen(
    monkeypatch: pytest.MonkeyPatch, auth: str | None
) -> None:
    monkeypatch.setenv("WITX_DATABASE_URL", "postgresql://x/y")
    if auth is None:
        monkeypatch.delenv("WITX_AUTH", raising=False)
    else:
        monkeypatch.setenv("WITX_AUTH", auth)  # misspelt: not taken for dev
    with pytest.raises(SystemExit, match="WITX_AUTH"):
        cli._serve_api(argparse.Namespace(host="127.0.0.1", port=0))
