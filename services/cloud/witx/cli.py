"""Command line: `witx migrate | api | worker | check | plugin ... | admin ...`."""

import argparse
import asyncio
import difflib
import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from witx_contracts import logs

from witx import telemetry
from witx.api.app import create_app
from witx.api.auth import Authenticator, DevAuthenticator, OidcAuthenticator, OidcLogin
from witx.config import Settings, plugins_dir
from witx.domain import rules
from witx.harness import plugins, skills
from witx.harness.compress import Compressor
from witx.harness.loop import Harness
from witx.harness.plugins import Fetched, Installed
from witx.harness.risk import RiskAssessor
from witx.harness.verify import GoalJudge
from witx.hub import Hub
from witx.models import ModelClient
from witx.models.chat import ChatCompletions
from witx.models.openai import OpenAIDecisions, OpenAIResponses
from witx.models.router import ModelRouter
from witx.objects import object_store_from_url
from witx.services import health as readiness_checks
from witx.services import identity as identity_service
from witx.services import ledger, recovery
from witx.store import devices, identity
from witx.store.db import Database, migrate
from witx.worker import (
    Worker,
    maintain_forever,
    relay_forever,
    schedule_forever,
    side_chat_forever,
    sweep_forever,
    title_forever,
)


def _authenticator(settings: Settings) -> Authenticator:
    """Checks the bearer tokens of programs (browsers use session cookies)."""
    if settings.auth == "oidc":
        assert settings.oidc_issuer, "WITX_OIDC_ISSUER is required"
        if not (settings.oidc_audience and settings.oidc_jwks_url):
            return DevAuthenticator({})  # browsers only: no bearer token is accepted
        return OidcAuthenticator(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
        )
    return DevAuthenticator(settings.dev_tokens)


def _login(settings: Settings) -> OidcLogin | None:
    """The Web app's OIDC sign-in, when a client is configured for it."""
    if not (settings.oidc_issuer and settings.oidc_client_id):
        return None
    return OidcLogin(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=f"{settings.public_url}/api/v1/auth/callback",
        audience=settings.oidc_audience,
    )


def _serve_api(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    db = Database(settings.database_url)
    objects = object_store_from_url(settings.object_store_url)
    app = create_app(
        settings,
        db=db,
        objects=objects,
        authenticator=_authenticator(settings),
        hub=Hub(db, objects, settings.limits),
        login=_login(settings),
    )
    telemetry.configure("api", app=app, engine=db.engine)
    # Our own request log (route, not URL) replaces uvicorn's; log_config=None keeps
    # uvicorn's records on our handler.
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_config=None)


DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai"


def _providers(settings: Settings) -> dict[str, ModelClient]:
    """A client for every provider the catalog uses; each needs its API key."""
    keys = {"openai": settings.openai_api_key, "deepinfra": settings.deepinfra_api_key}
    providers: dict[str, ModelClient] = {}
    for provider in {m.provider for m in settings.catalog().values()}:
        key = keys[provider]
        if not key:
            sys.exit(f"{provider.upper()}_API_KEY is required: WITX_MODELS uses {provider}")
        if provider == "openai":
            providers[provider] = OpenAIResponses(
                key, base_url=settings.openai_base_url, reasoning_effort=settings.reasoning_effort
            )
        else:
            providers[provider] = ChatCompletions(
                key, base_url=DEEPINFRA_URL, reasoning_effort=settings.reasoning_effort
            )
    return providers


# The models the Decisions API serves, by the provider's own ID.
DECISION_MODELS = {"gpt-6-luna"}


def _decisions(settings: Settings) -> dict[str, Any]:
    """The fast risk check's client and the catalog models it serves; nothing without
    an OpenAI key. Tenants still choose whether to use it."""
    served = {
        m.name: m.provider_model
        for m in settings.catalog().values()
        if m.provider == "openai" and m.provider_model in DECISION_MODELS
    }
    if not settings.openai_api_key or not served:
        return {}
    client = OpenAIDecisions(settings.openai_api_key, base_url=settings.openai_base_url)
    return {"decisions": client, "decision_models": served}


async def _check() -> int:
    """What a worker needs to do its work : the API's checks plus each model
    of the catalog, through the provider's free model list (no inference)."""
    settings = Settings.from_env()
    db = Database(settings.database_url)
    try:
        checks = await readiness_checks.readiness(
            db, object_store_from_url(settings.object_store_url)
        )
    finally:
        await db.close()
    router = ModelRouter(settings.catalog(), _providers(settings), settings.default_model)
    for name in settings.catalog():
        checks[f"model {name}"] = await router.ready(name)
    for name, plugin in Installed(plugins_dir()).load().items():
        checks[f"plugin {name}"] = plugin if isinstance(plugin, str) else "ok"
    print(json.dumps(checks, ensure_ascii=False))
    return 0 if all(v == "ok" for v in checks.values()) else 1


async def _run_worker(concurrency: int) -> None:
    settings = Settings.from_env()
    db = Database(settings.database_url)
    telemetry.configure("worker", engine=db.engine)
    model = ModelRouter(settings.catalog(), _providers(settings), settings.default_model)
    objects = object_store_from_url(settings.object_store_url)
    harness = Harness(
        db,
        model,
        objects,
        settings.limits,
        RiskAssessor(
            model,
            timeout=settings.limits.risk_timeout.total_seconds(),
            default_provider=settings.catalog()[settings.default_model].provider,
            **_decisions(settings),
        ),
        catalog=settings.catalog(),
        default_model=settings.default_model,
        judge=GoalJudge(model),
        compressor=Compressor(model),
        plugins=Installed(plugins_dir()),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await asyncio.gather(
            Worker(db, harness, concurrency=concurrency).run_forever(stop),
            maintain_forever(
                db,
                objects,
                stop,
                screenshot_retention=settings.screenshot_retention,
                audit_retention=settings.audit_retention,
            ),
            sweep_forever(db, objects, stop),
            schedule_forever(db, stop),
            title_forever(db, model, settings.default_model, stop),
            side_chat_forever(db, model, objects, stop),
            relay_forever(harness, stop),
        )
    finally:
        await db.close()


def _plugin(args: argparse.Namespace) -> int:
    """Plugins: nothing is installed or changed without a person seeing it first."""
    installed = Installed(plugins_dir())
    try:
        if args.plugin_command == "check":
            found = plugins.check(Path(args.path))
            for problem in found:
                print(problem)
            print("ok" if not found else f"{len(found)} problem(s)")
            return 1 if found else 0
        if args.plugin_command == "list":
            for name, plugin in installed.load().items():
                entry = installed.locked()[name]
                state = "ok" if not isinstance(plugin, str) else f"not loaded: {plugin}"
                where = f"{entry.marketplace} at {entry.commit[:12]}"
                print(f"{name} {entry.version} from {where}: {state}")
            return 0
        if args.plugin_command == "install":
            name, _, marketplace = args.plugin.partition("@")
            with installed.fetch(name, marketplace or plugins.OFFICIAL) as fetched:
                if not _fits(fetched):
                    return 1
                print(_describe(fetched))
                for file in skills.files(fetched.folder):
                    if file.endswith("SKILL.md"):
                        print(f"\n--- {file}\n{(fetched.folder / file).read_text('utf-8')}")
                if not _confirm(f"Install {name} {fetched.plugin.version}?", args.yes):
                    return 1
                installed.install(fetched)
            print(f"installed {name} {fetched.plugin.version}")
            return 0
        if args.plugin_command == "update":
            current = installed.locked().get(args.name)
            if current is None:
                raise ValueError(f"no plugin named {args.name} is installed")
            with installed.fetch(args.name, current.marketplace) as fetched:
                if fetched.locked.sha256 == current.sha256:
                    print(f"{args.name} {current.version} is up to date")
                    return 0
                if not _fits(fetched):
                    return 1
                print(_describe(fetched))
                print(f"was {current.version} at {current.commit[:12]}")
                print(_diff(installed.folder(args.name), fetched.folder))
                question = f"Update {args.name} to {fetched.plugin.version}?"
                if not _confirm(question, args.yes):
                    return 1
                installed.install(fetched)
            print(f"updated {args.name} to {fetched.plugin.version}")
            return 0
        if args.plugin_command == "remove":
            installed.remove(args.name)
            print(f"removed {args.name}")
            return 0
        if args.marketplace_command == "add":
            print(f"added marketplace {installed.add_marketplace(args.source)}")
        elif args.marketplace_command == "remove":
            installed.remove_marketplace(args.name)
            print(f"removed marketplace {args.name}")
        else:
            for name, source in installed.marketplaces().items():
                print(f"{name}: {source}")
        return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _fits(fetched: Fetched) -> bool:
    found = plugins.problems(fetched.plugin)
    for problem in found:
        print(f"error: {problem}", file=sys.stderr)
    return not found


def _describe(fetched: Fetched) -> str:
    plugin, entry = fetched.plugin, fetched.locked
    where = f"{entry.path} in " if entry.path else ""
    return (
        f"{plugin.name} {plugin.version}: {plugin.description}\n"
        f"from {entry.marketplace}: {where}{entry.source} at {entry.commit}\n"
        f"skills: {', '.join(s.name for s in plugin.skills)}\n"
        "A plugin is text the agent reads: it runs no code and grants no permission, but "
        "the agent follows it. Read it before you install it."
    )


def _diff(old: Path, new: Path) -> str:
    lines: list[str] = []
    for name in sorted(set(skills.files(old)) | set(skills.files(new))):
        before = (old / name).read_text("utf-8").splitlines() if (old / name).is_file() else []
        after = (new / name).read_text("utf-8").splitlines() if (new / name).is_file() else []
        lines += difflib.unified_diff(before, after, f"a/{name}", f"b/{name}", lineterm="")
    return "\n".join(lines)


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _admin_url() -> str:
    return os.environ.get("WITX_ADMIN_DATABASE_URL") or os.environ["WITX_DATABASE_URL"]


async def _add_user(tenant: str, subject: str, name: str, role: str) -> None:
    db = Database(_admin_url())
    try:
        async with db.system() as conn:
            tenant_id = await identity.ensure_tenant(conn, tenant)
            user_id = await identity.ensure_user(conn, subject, name)
            await identity.ensure_membership(conn, tenant_id, user_id, role)
    finally:
        await db.close()
    print(f"user {subject} is {role} of tenant {tenant} ({tenant_id})")


async def _add_gateway(tenant: str, name: str) -> None:
    db = Database(_admin_url())
    try:
        async with db.system() as conn:
            tenant_id = await identity.ensure_tenant(conn, tenant)
            gateway_id, code = await identity_service.create_gateway(
                conn, tenant_id=tenant_id, name=name, now=datetime.now(UTC)
            )
    finally:
        await db.close()
    hours = int(rules.ENROLL_CODE_TTL.total_seconds() // 3600)
    print(
        f"gateway {name} ({gateway_id}) created. On its host, within {hours} hours, run\n"
        f"  witx-gateway --config gateway.toml enroll\n"
        f"and paste this one-time code:\n{code}"
    )


async def _list_gateways() -> None:
    db = Database(_admin_url())
    try:
        async with db.system() as conn:
            rows = await devices.list_gateways(conn)
    finally:
        await db.close()
    for row in rows:
        if row["revoked_at"]:
            state = "revoked"
        elif row["disabled_at"]:
            state = "disabled"
        elif row["enrolled_at"]:
            state = "enrolled"
        else:
            state = f"waiting to enroll until {row['enroll_expires_at']:%Y-%m-%d %H:%M}"
        print(f"{row['id']}  {row['tenant']}/{row['name']}  host={row['host'] or '-'}  {state}")


async def _recover() -> None:
    db = Database(_admin_url())
    objects = object_store_from_url(Settings.from_env().object_store_url)
    try:
        done = await recovery.recover(db, objects, datetime.now(UTC))
    finally:
        await db.close()
    print("recovered: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in done.items()))
    print(
        "Gateways whose credential was rotated after the backup must enroll again; "
        "settle the unknown inputs from new screenshots before their devices take tasks."
    )


async def _change_gateway(gateway_id: UUID, change: str) -> None:
    db = Database(_admin_url())
    objects = object_store_from_url(Settings.from_env().object_store_url)
    try:
        async with db.system() as conn:
            if await devices.gateway_known(conn, gateway_id):
                # In the restore ledger first: an older backup must not undo this.
                state = {"enable": "enabled", "disable": "disabled", "revoke": "revoked"}[change]
                await ledger.record(objects, "gateway", gateway_id, {"state": state})
            if change == "revoke":
                changed = await devices.revoke_gateway(conn, gateway_id)
            else:
                changed = await devices.set_gateway_disabled(conn, gateway_id, change == "disable")
    finally:
        await db.close()
    if not changed:
        raise SystemExit(f"no gateway {gateway_id} that can be changed (unknown or revoked)")
    after = "" if change == "enable" else " (if connected, it is cut off at its next heartbeat)"
    print(f"gateway {gateway_id}: {change}d{after}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="witx")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations (uses WITX_ADMIN_DATABASE_URL)")
    sub.add_parser(
        "check",
        help="readiness of a worker: database, object store and every catalog model "
        "(exit code 1 if any is not ok)",
    )
    p_api = sub.add_parser("api", help="serve the HTTP API and the Gateway hub")
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8000)
    p_worker = sub.add_parser("worker", help="run the agent worker")
    p_worker.add_argument("--concurrency", type=int, default=4)
    p_plugin = sub.add_parser("plugin", help="the plugins the agent's skills come from")
    plugin = p_plugin.add_subparsers(dest="plugin_command", required=True)
    plugin.add_parser("list", help="installed plugins, and whether each is loaded")
    p_install = plugin.add_parser("install", help="shows the plugin, installs it once confirmed")
    p_install.add_argument("plugin", help="name@marketplace (the official one without @)")
    p_install.add_argument("--yes", action="store_true", help="do not ask")
    p_update = plugin.add_parser("update", help="shows what changed, updates once confirmed")
    p_update.add_argument("name")
    p_update.add_argument("--yes", action="store_true", help="do not ask")
    plugin.add_parser("remove").add_argument("name")
    p_check = plugin.add_parser(
        "check", help="checks a plugin or a marketplace folder against this build"
    )
    p_check.add_argument("path")
    p_market = plugin.add_parser("marketplace", help="where plugins are installed from")
    market = p_market.add_subparsers(dest="marketplace_command", required=True)
    market.add_parser("list")
    market.add_parser("add", help="a git URL or a local repository").add_argument("source")
    market.add_parser("remove").add_argument("name")
    p_admin = sub.add_parser("admin", help="provisioning")
    admin = p_admin.add_subparsers(dest="admin_command", required=True)
    p_user = admin.add_parser("add-user")
    p_user.add_argument("--tenant", required=True)
    p_user.add_argument("--subject", required=True, help="OIDC subject or dev subject")
    p_user.add_argument("--name", required=True)
    p_user.add_argument("--role", choices=["owner", "member"], default="owner")
    p_gateway = admin.add_parser("add-gateway", help="prints a one-time enrollment code")
    p_gateway.add_argument("--tenant", required=True)
    p_gateway.add_argument("--name", required=True)
    admin.add_parser("list-gateways")
    admin.add_parser(
        "recover",
        help="after restoring the database from a backup: replay the ledger, stop what was "
        "in flight (run before starting the API and workers)",
    )
    for change, what in (
        ("disable", "stop a Gateway until it is enabled again"),
        ("enable", "let a disabled Gateway connect again"),
        ("revoke", "end a Gateway for good (it must enroll anew as a new Gateway)"),
    ):
        p_change = admin.add_parser(f"{change}-gateway", help=what)
        p_change.add_argument("gateway_id", type=UUID)
    args = parser.parse_args()
    logs.configure(args.command)

    if args.command == "migrate":
        applied = asyncio.run(migrate(_admin_url()))
        print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    elif args.command == "check":
        sys.exit(asyncio.run(_check()))
    elif args.command == "api":
        _serve_api(args)
    elif args.command == "worker":
        asyncio.run(_run_worker(args.concurrency))
    elif args.command == "plugin":
        sys.exit(_plugin(args))
    elif args.admin_command == "add-user":
        asyncio.run(_add_user(args.tenant, args.subject, args.name, args.role))
    elif args.admin_command == "add-gateway":
        asyncio.run(_add_gateway(args.tenant, args.name))
    elif args.admin_command == "recover":
        asyncio.run(_recover())
    elif args.admin_command == "list-gateways":
        asyncio.run(_list_gateways())
    elif args.admin_command.endswith("-gateway"):
        asyncio.run(_change_gateway(args.gateway_id, args.admin_command.removesuffix("-gateway")))
