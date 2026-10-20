"""WitX as an MCP server: AI clients (Claude, Codex, ...) give tasks to the user's phones.

Streamable HTTP at /api/v1/mcp, signed in with the same Bearer tokens as the API, acting
as that user in their tenant (``X-Witx-Tenant`` picks one if they have several).

Approvals stay with people. A client cannot approve by calling a tool:
``review_approval`` asks the person through MCP elicitation, a form the client shows to
the user, never to the model. Payments are approved on the Web app only, and clients
without elicitation get the Web link. Denying needs no person: it only makes things safer.
Full access cannot be turned on here.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from witx.api import views
from witx.api.auth import Authenticator
from witx.config import Settings
from witx.domain import rules
from witx.domain.model import ActionStatus, PermissionMode
from witx.domain.rules import Rejected
from witx.objects import ObjectStore
from witx.services import execution as exec_service
from witx.services import identity, lifecycle
from witx.services.identity import Actor
from witx.store import execution, tasks
from witx.store import identity as identity_store
from witx.store.db import Database

log = logging.getLogger(__name__)

PATH = "/api/v1/mcp"

INSTRUCTIONS = """WitX operates the user's own Android and iOS phones through an agent that
looks at screenshots and taps, swipes and types. Give it a goal with create_task, follow
it with get_task, and answer its questions with add_input. Inputs with sensitive effects
wait for a person's approval: call review_approval so the user can decide in this client
(payments only on the WitX Web app). Never claim an input was approved unless
review_approval says so."""


class BearerVerifier:
    """The API's own Bearer tokens (developer tokens or OIDC access tokens)."""

    def __init__(self, authenticator: Authenticator) -> None:
        self.authenticator = authenticator

    async def verify_token(self, token: str) -> AccessToken | None:
        subject = await self.authenticator.subject(token)
        if subject is None:
            return None
        return AccessToken(token=token, client_id="witx", scopes=[], subject=subject)


class Decision(BaseModel):
    """The form a person fills in the client to decide on one held input."""

    # Required and without a default: pressing Accept on an untouched form decides nothing.
    decision: Literal["approve", "deny"] = Field(
        title="Decision", description="Choose approve or deny, then press Accept to send it"
    )


def build(
    settings: Settings, *, db: Database, objects: ObjectStore, authenticator: Authenticator
) -> MCPServer:
    server = MCPServer(
        "WitX",
        instructions=INSTRUCTIONS,
        token_verifier=BearerVerifier(authenticator),
        auth=AuthSettings(
            issuer_url=settings.oidc_issuer or settings.public_url,
            resource_server_url=f"{settings.public_url}{PATH}",
            # The Authenticator checks an OIDC access token's audience itself.
            validate_token_resource=False,
        ),
    )

    def web(task_id: UUID) -> str:
        return f"{settings.public_url}/tasks/{task_id}"

    async def actor(ctx: Context) -> Actor:
        token = get_access_token()
        if token is None or token.subject is None:
            raise Rejected("unauthenticated")
        request = ctx.request_context.request
        header = request.headers.get("x-witx-tenant") if request is not None else None
        async with db.system() as conn:
            return await identity.actor_for(conn, token.subject, UUID(header) if header else None)

    async def task_state(who: Actor, task_id: UUID) -> dict[str, Any]:
        """What a client needs to follow a task: status, result, question, held inputs."""
        now = datetime.now(UTC)
        async with db.tenant(who.tenant_id) as conn:
            task = await tasks.get(conn, task_id)
            if task is None:
                raise Rejected("not_found", "task not found")
            detail = await views.task_detail(conn, task)
            held = []
            for action in await execution.task_actions(conn, task.id):
                if action.status != ActionStatus.AWAITING_APPROVAL:
                    continue
                approval = await execution.get_approval(conn, action.id)
                held.append(
                    {
                        "action_id": str(action.id),
                        "input": action.input,
                        "target": action.target,
                        "expected_effect": action.expected_effect,
                        "reasons": action.policy.reasons if action.policy else [],
                        "screenshot_id": str(action.observation_id),
                        "expires_at": approval.expires_at.isoformat() if approval else None,
                        "approve_where": (
                            "web" if exec_service.needs_web_approval(action) else "here or web"
                        ),
                    }
                )
        question = next(
            (r.wait_detail for r in detail.runs if r.wait_reason == "user_input" and r.wait_detail),
            None,
        )
        return {
            "task_id": str(task.id),
            "status": detail.status,
            "request": detail.request,
            "goal": detail.goal,
            "model": detail.model,
            "permission_mode": detail.permission_mode,
            "question": question,
            "awaiting_approval": held,
            "result": detail.result,
            "latest_screenshots": [
                str(o.id) for o in detail.observations if o.status == "complete"
            ][-4:],
            "usage": detail.usage.model_dump(),
            "web_url": web(task.id),
            "checked_at": now.isoformat(),
        }

    @server.tool()
    async def list_devices(ctx: Context) -> list[dict[str, Any]]:
        """The user's phones: ID, name, platform, whether online, the apps it may open, and
        the task using it (if any)."""
        who = await actor(ctx)
        async with db.tenant(who.tenant_id) as conn:
            return [d.model_dump(mode="json") for d in await views.device_views(conn)]

    @server.tool()
    async def create_task(
        ctx: Context,
        goal: str,
        device_ids: list[str],
        constraints: list[str] | None = None,
        model: str | None = None,
        permission_mode: Literal["ask", "auto"] = "auto",
    ) -> dict[str, Any]:
        """Give the phones a goal. permission_mode "auto" asks a person before sensitive
        inputs; "ask" before any input that changes something (looking around runs).
        Returns the task's state."""
        who = await actor(ctx)
        chosen = model or settings.default_model
        rules.check_model(chosen, settings.catalog())
        async with db.tenant(who.tenant_id) as conn:
            default_provider = settings.catalog()[settings.default_model].provider
            allowed = rules.allowed_providers(
                await identity_store.allowed_providers(conn, who.tenant_id), default_provider
            )
            rules.check_provider(settings.catalog()[chosen].provider, allowed)
            task = await lifecycle.create_task(
                conn,
                tenant_id=who.tenant_id,
                user_id=who.user_id,
                request=goal,
                constraints=constraints or [],
                device_ids=list(dict.fromkeys(UUID(d) for d in device_ids)),
                model=chosen,
                permission_mode=PermissionMode(permission_mode),
            )
        return await task_state(who, task.id)

    @server.tool()
    async def get_task(ctx: Context, task_id: str) -> dict[str, Any]:
        """A task's status, its question to the user if it has one, the inputs waiting
        for approval, its result and its newest screenshots."""
        return await task_state(await actor(ctx), UUID(task_id))

    @server.tool()
    async def add_input(ctx: Context, task_id: str, text: str) -> dict[str, Any]:
        """Send the task a message: answer its question, or ask for something else. A
        message starting with /goal sets the task's goal; /plan <what to do> asks the agent
        to present a plan first, and /plan approve lets it act on the plan it presented."""
        who = await actor(ctx)
        async with db.tenant(who.tenant_id) as conn:
            await lifecycle.add_input(conn, task_id=UUID(task_id), user_id=who.user_id, text=text)
        return await task_state(who, UUID(task_id))

    @server.tool()
    async def control_task(
        ctx: Context, task_id: str, action: Literal["pause", "resume", "stop"]
    ) -> dict[str, Any]:
        """Pause, resume or stop a task. Pausing and stopping finish once every device has
        confirmed; get_task shows when."""
        who = await actor(ctx)
        async with db.tenant(who.tenant_id) as conn:
            if action == "pause":
                await lifecycle.pause(conn, UUID(task_id))
            elif action == "resume":
                await lifecycle.resume(conn, UUID(task_id))
            else:
                await lifecycle.stop(conn, UUID(task_id))
        return await task_state(who, UUID(task_id))

    @server.tool()
    async def get_screenshot(ctx: Context, screenshot_id: str) -> Image:
        """A screenshot the task took (IDs come from get_task)."""
        who = await actor(ctx)
        async with db.tenant(who.tenant_id) as conn:
            obs = await execution.get_observation(conn, UUID(screenshot_id))
            hidden = (
                obs is not None
                and obs.task_id is not None
                and (await tasks.get(conn, obs.task_id) is None)
            )
        if obs is None or hidden or obs.object_key is None:
            raise Rejected("not_found", "screenshot not found or deleted")
        return Image(data=await objects.get(obs.object_key), format="png")

    @server.tool()
    async def deny_action(ctx: Context, action_id: str) -> dict[str, Any]:
        """Deny an input that waits for approval, only when the user tells you to. The agent
        is told and continues."""
        who = await actor(ctx)
        async with db.tenant(who.tenant_id) as conn:
            action = await exec_service.decide(
                conn,
                action_id=UUID(action_id),
                decision="deny",
                user_id=who.user_id,
                now=datetime.now(UTC),
                via="mcp",
                client=client_name(ctx),
            )
        return {"action_id": action_id, "status": action.status.value}

    async def ask_person(ctx: Context, action_id: str) -> Elicit[Decision] | None:
        """The question review_approval puts to the person, or None when there is none
        to ask here (not waiting, a payment, or a client that cannot show a form). The
        SDK delivers it as the client's protocol requires: a request during the call, or
        (2026-07-28) an input-required result that the client answers and retries."""
        who = await actor(ctx)
        async with db.tenant(who.tenant_id) as conn:
            action = await execution.get_action(conn, UUID(action_id))
        log.info(
            "approval asked over MCP",
            extra={
                "action_id": action_id,
                "protocol": ctx.protocol_version,
                "client_can_show_form": can_ask_user(ctx),
                "client": client_name(ctx),
            },
        )
        if (
            action is None
            or action.status != ActionStatus.AWAITING_APPROVAL
            or exec_service.needs_web_approval(action)
            or not can_ask_user(ctx)
        ):
            return None
        return Elicit(approval_message(action, web(action.task_id)), Decision)

    @server.tool()
    async def review_approval(
        ctx: Context,
        action_id: str,
        # Spelled out (not ElicitationResult[...] | None) so the SDK injects the whole
        # outcome, decline and cancel included, instead of the bare form data.
        answer: Annotated[
            AcceptedElicitation[Decision] | DeclinedElicitation | CancelledElicitation | None,
            Resolve(ask_person),
        ] = None,
    ) -> dict[str, Any]:
        """Let the user approve or deny an input that waits for approval. The user decides
        in a form this client shows them; you cannot answer it. Payments, and clients
        without that form, get the Web link instead."""
        who = await actor(ctx)
        async with db.tenant(who.tenant_id) as conn:
            action = await execution.get_action(conn, UUID(action_id))
            if action is None:
                raise Rejected("not_found", "input not found")
        url = web(action.task_id)
        if action.status != ActionStatus.AWAITING_APPROVAL:
            return {"action_id": action_id, "status": action.status.value}
        # Only a form the person filled in decides. A decline or cancel is not one: some
        # clients decline at once without showing the form. When nothing was asked, the
        # SDK still hands over an "accepted" outcome without data: not a decision either.
        decision = None
        if isinstance(answer, AcceptedElicitation) and isinstance(answer.data, Decision):
            decision = answer.data.decision
        if decision is None:
            if exec_service.needs_web_approval(action):
                why = "payments are approved on the WitX Web app only"
            elif isinstance(answer, DeclinedElicitation | CancelledElicitation):
                why = (
                    "the approval form was declined or closed, possibly by the client without "
                    "showing it; nothing was decided. Do not deny on the user's behalf"
                )
            else:
                why = "this client cannot show an approval form to the user"
            return {
                "action_id": action_id,
                "status": "awaiting_approval",
                "approve_on_web": url,
                "why": why,
            }
        async with db.tenant(who.tenant_id) as conn:
            decided = await exec_service.decide(
                conn,
                action_id=action.id,
                decision=decision,
                user_id=who.user_id,
                now=datetime.now(UTC),
                via="mcp",
                client=client_name(ctx),
            )
        return {"action_id": action_id, "status": decided.status.value, "decided_by": "user"}

    return server


def can_ask_user(ctx: Context) -> bool:
    """Whether the client declared it can show an elicitation form to the user."""
    capabilities = ctx.client_capabilities
    return capabilities is not None and capabilities.elicitation is not None


def client_name(ctx: Context) -> str | None:
    params = ctx.request_context.session.client_params
    info = params.client_info if params is not None else None
    return info.name[:100] if info is not None else None


TERM_LABELS = {
    "sender": "From",
    "recipient": "To",
    "body": "Message",
    "payee": "Pay to",
    "amount": "Amount",
    "currency": "Currency",
    "address": "Deliver to",
    "payment_method": "Pay with",
}


def approval_message(action: Any, url: str) -> str:
    reasons = "; ".join(action.policy.reasons) if action.policy else "held for approval"
    details = {k: v for k, v in action.input.items() if k not in ("kind",)}
    terms = "".join(f"{TERM_LABELS[k]}: {v}\n" for k, v in (action.intent or {}).items())
    return (
        f"WitX: approve this input on your phone?\n\n"
        f"{terms}"
        f"Input: {action.input.get('kind')} {details}\n"
        f"On: {action.target}\n"
        f"Expected: {action.expected_effect}\n"
        f"Why it waits: {reasons}\n\n"
        f"See the screenshot with the input marked: {url}\n\n"
        "Choose approve or deny under Decision, then Accept to send it."
    )


def app(server: MCPServer) -> Starlette:
    """The ASGI app serving PATH. Bearer tokens are required, so the DNS-rebinding
    guard (meant for unauthenticated local servers) is off; Origin is not a credential."""
    return server.streamable_http_app(
        streamable_http_path=PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
