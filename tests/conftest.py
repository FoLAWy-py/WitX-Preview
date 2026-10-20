"""Shared fixtures.

Database tests need PostgreSQL 15+. Either set WITX_TEST_DATABASE_URL to a server
where that role may create databases and roles (e.g.
``postgresql://postgres@localhost/postgres``), or have ``initdb``/``pg_ctl`` on PATH
and run as a non-root user: a throwaway cluster is then started automatically.
Without either, database tests are skipped.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from witx.store.db import Database, async_url, migrate
from witx_contracts import logs

APP_ROLE = "witx_test_app"


def _start_cluster(root: Path) -> str:
    data = root / "data"
    subprocess.run(
        ["initdb", "-D", str(data), "-U", "witx_owner", "--auth=trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "pg_ctl",
            "-D",
            str(data),
            "-l",
            str(root / "log"),
            "-w",
            "start",
            "-o",
            f"-k {root} -c listen_addresses='' -p 54329",
        ],
        check=True,
        capture_output=True,
    )
    return f"postgresql://witx_owner@/postgres?host={quote(str(root))}&port=54329"


@pytest.fixture(scope="session")
def server_url() -> Iterator[str]:
    url = os.environ.get("WITX_TEST_DATABASE_URL")
    if url:
        yield url
        return
    if not shutil.which("initdb") or os.geteuid() == 0:
        pytest.skip("no PostgreSQL available (set WITX_TEST_DATABASE_URL)")
    root = Path(tempfile.mkdtemp(prefix="witx-pg-", dir="/tmp"))
    try:
        yield _start_cluster(root)
    finally:
        subprocess.run(
            ["pg_ctl", "-D", str(root / "data"), "-m", "immediate", "stop"], capture_output=True
        )
        shutil.rmtree(root, ignore_errors=True)


def _with_database(url: str, name: str, user: str | None = None) -> str:
    base, _, query = url.partition("?")
    scheme, _, rest = base.partition("://")
    auth, _, _ = rest.rpartition("/")
    if user:
        host = auth.split("@", 1)[-1]
        auth = f"{user}@{host}"
    return f"{scheme}://{auth}/{name}" + (f"?{query}" if query else "")


async def _admin(url: str, *statements: str) -> None:
    engine = create_async_engine(async_url(url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def template(server_url: str) -> Iterator[str]:
    """A migrated template database; each test copies it."""
    name = f"witx_template_{uuid.uuid4().hex[:8]}"
    asyncio.run(
        _admin(
            server_url,
            f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'"
            " LC_COLLATE 'C' LC_CTYPE 'C'",
        )
    )
    asyncio.run(migrate(_with_database(server_url, name)))
    asyncio.run(
        _admin(
            server_url,
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') "
            f"THEN CREATE ROLE {APP_ROLE} LOGIN; END IF; END $$",
            f"GRANT witx_app TO {APP_ROLE}",
        )
    )
    yield name
    asyncio.run(_admin(server_url, f'DROP DATABASE IF EXISTS "{name}"'))


@dataclass(frozen=True)
class Databases:
    admin_url: str  # schema owner: provisioning only
    app_url: str  # application role: subject to row-level security


@pytest.fixture
async def databases(server_url: str, template: str) -> AsyncIterator[Databases]:
    name = f"witx_test_{uuid.uuid4().hex[:12]}"
    await _admin(server_url, f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    try:
        yield Databases(
            admin_url=_with_database(server_url, name),
            app_url=_with_database(server_url, name, APP_ROLE),
        )
    finally:
        await _admin(server_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def db(databases: Databases) -> AsyncIterator[Database]:
    database = Database(databases.app_url, pool_size=5)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def admin_db(databases: Databases) -> AsyncIterator[Database]:
    database = Database(databases.admin_url, pool_size=2)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def log_lines() -> Iterator[Callable[[], list[dict[str, object]]]]:
    """Captures what the processes would write (JSON, redacted) and parses it."""
    stream = io.StringIO()
    capture = logs.handler("test", stream)
    root = logging.getLogger()
    level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        root.removeHandler(capture)
        root.setLevel(level)
