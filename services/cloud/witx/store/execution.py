"""Observations, actions and approvals."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import (
    Action,
    ActionStatus,
    Approval,
    Effect,
    Grant,
    Observation,
    ObservationStatus,
    PermissionMode,
    PermissionRequest,
    Policy,
    RiskVerdict,
    Screen,
)
from witx.store.db import execute, fetch, fetch_one, jsonb

# ---------------------------------------------------------------- observations

_OBS = """id, tenant_id, device_id, task_id, epoch, status, requested_at, captured_at, screen,
          sha256, object_key"""


def _observation(row: RowMapping) -> Observation:
    screen = row["screen"]
    return Observation(
        id=row["id"],
        tenant_id=row["tenant_id"],
        device_id=row["device_id"],
        task_id=row["task_id"],
        epoch=row["epoch"],
        status=ObservationStatus(row["status"]),
        requested_at=row["requested_at"],
        captured_at=row["captured_at"],
        screen=Screen(**screen) if screen else None,
        sha256=row["sha256"],
        object_key=row["object_key"],
    )


async def request_observation(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    device_id: UUID,
    task_id: UUID | None,
    epoch: int | None,
) -> Observation:
    row = await fetch_one(
        conn,
        f"""INSERT INTO observations (id, tenant_id, device_id, task_id, epoch, status)
            VALUES (:id, :t, :d, :task, :epoch, 'requested') RETURNING {_OBS}""",
        id=id,
        t=tenant_id,
        d=device_id,
        task=task_id,
        epoch=epoch,
    )
    assert row is not None
    return _observation(row)


async def get_observation(conn: AsyncConnection, obs_id: UUID) -> Observation | None:
    row = await fetch_one(conn, f"SELECT {_OBS} FROM observations WHERE id = :id", id=obs_id)
    return _observation(row) if row else None


async def observations(conn: AsyncConnection, ids: list[UUID]) -> list[Observation]:
    rows = await fetch(conn, f"SELECT {_OBS} FROM observations WHERE id = ANY(:ids)", ids=ids)
    return [_observation(r) for r in rows]


async def task_observations(conn: AsyncConnection, task_id: UUID) -> list[Observation]:
    rows = await fetch(
        conn,
        f"SELECT {_OBS} FROM observations WHERE task_id = :t ORDER BY requested_at",
        t=task_id,
    )
    return [_observation(r) for r in rows]


async def first_observation(
    conn: AsyncConnection, *, task_id: UUID, device_id: UUID, epoch: int, since: datetime
) -> Observation | None:
    """The screenshot taken of a device as a run of the task started: the first one
    requested under the current lease epoch since then."""
    row = await fetch_one(
        conn,
        f"""SELECT {_OBS} FROM observations
             WHERE task_id = :t AND device_id = :d AND epoch = :e AND requested_at >= :since
             ORDER BY requested_at LIMIT 1""",
        t=task_id,
        d=device_id,
        e=epoch,
        since=since,
    )
    return _observation(row) if row else None


async def latest_complete(conn: AsyncConnection, device_id: UUID) -> Observation | None:
    row = await fetch_one(
        conn,
        f"""SELECT {_OBS} FROM observations
             WHERE device_id = :d AND status = 'complete'
             ORDER BY captured_at DESC LIMIT 1""",
        d=device_id,
    )
    return _observation(row) if row else None


async def observations_to_send(conn: AsyncConnection, device_ids: list[UUID]) -> list[Observation]:
    """Requested observations, plus ones sent earlier that never completed (resend)."""
    rows = await fetch(
        conn,
        f"""SELECT {_OBS} FROM observations
             WHERE device_id = ANY(:d) AND status = 'requested' ORDER BY requested_at""",
        d=device_ids,
    )
    return [_observation(r) for r in rows]


async def unfinished_observations(
    conn: AsyncConnection, device_ids: list[UUID]
) -> list[Observation]:
    rows = await fetch(
        conn,
        f"""SELECT {_OBS} FROM observations
             WHERE device_id = ANY(:d) AND status = 'sent' ORDER BY requested_at""",
        d=device_ids,
    )
    return [_observation(r) for r in rows]


async def mark_observation_sent(conn: AsyncConnection, obs_id: UUID) -> None:
    await execute(
        conn,
        "UPDATE observations SET status = 'sent', sent_at = now()"
        " WHERE id = :id AND status = 'requested'",
        id=obs_id,
    )


async def complete_observation(
    conn: AsyncConnection,
    obs_id: UUID,
    *,
    captured_at: datetime,
    screen: dict[str, Any],
    sha256: str,
    object_key: str,
) -> bool:
    count = await execute(
        conn,
        """UPDATE observations SET status = 'complete', captured_at = :at,
                  screen = CAST(:screen AS jsonb), sha256 = :sha, object_key = :key
            WHERE id = :id AND status IN ('requested', 'sent')""",
        id=obs_id,
        at=captured_at,
        screen=jsonb(screen),
        sha=sha256,
        key=object_key,
    )
    return count == 1


async def fail_observation(conn: AsyncConnection, obs_id: UUID, error: str) -> bool:
    count = await execute(
        conn,
        """UPDATE observations SET status = 'failed', error = :e
            WHERE id = :id AND status IN ('requested', 'sent')""",
        id=obs_id,
        e=error,
    )
    return count == 1


async def fail_open_observations(conn: AsyncConnection, device_ids: list[UUID], error: str) -> None:
    await execute(
        conn,
        """UPDATE observations SET status = 'failed', error = :e
            WHERE device_id = ANY(:d) AND status IN ('requested', 'sent')""",
        d=device_ids,
        e=error,
    )


# ---------------------------------------------------------------- actions

_ACTION = """id, tenant_id, task_id, run_id, device_id, tool_call_id, epoch, control_revision,
             observation_id, kind, input, target, expected_effect, effects, digest, status,
             effect, reason, post_observation_id, created_at, sent_at, finished_at, policy,
             found_apps, intent, reconciled_by, retry_of, purchase_intent_id, field_text,
             field_info, changes"""


def policy_json(policy: Policy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    risk = policy.risk
    return {
        "mode": policy.mode.value,
        "decision": policy.decision,
        "reasons": policy.reasons,
        "declared_effects": policy.declared_effects,
        "risk": None
        if risk is None
        else {
            "sensitive": risk.sensitive,
            "effects": risk.effects,
            "reason": risk.reason,
            "failed": risk.failed,
            "seen": risk.seen,
            "permission": None
            if risk.permission is None
            else {
                "category": risk.permission.category,
                "grants": risk.permission.grants,
                "broad": risk.permission.broad,
            },
        },
        "app_sensitive": policy.app_sensitive,
        "text_flags": policy.text_flags,
        "found_rank": policy.found_rank,
    }


def _policy(value: dict[str, Any] | None) -> Policy | None:
    if not value:
        return None
    risk = value.get("risk")
    return Policy(
        mode=PermissionMode(value["mode"]),
        decision=value["decision"],
        reasons=list(value["reasons"]),
        declared_effects=list(value["declared_effects"]),
        risk=None
        if risk is None
        else RiskVerdict(
            sensitive=risk["sensitive"],
            effects=list(risk["effects"]),
            reason=risk["reason"],
            failed=risk["failed"],
            seen=risk.get("seen"),
            permission=PermissionRequest(**risk["permission"]) if risk.get("permission") else None,
        ),
        app_sensitive=value["app_sensitive"],
        text_flags=list(value["text_flags"]),
        found_rank=value.get("found_rank"),
    )


def _action(row: RowMapping) -> Action:
    return Action(
        id=row["id"],
        tenant_id=row["tenant_id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        device_id=row["device_id"],
        tool_call_id=row["tool_call_id"],
        epoch=row["epoch"],
        control_revision=row["control_revision"],
        observation_id=row["observation_id"],
        kind=row["kind"],
        input=dict(row["input"]),
        target=row["target"],
        expected_effect=row["expected_effect"],
        effects=list(row["effects"]),
        digest=row["digest"],
        status=ActionStatus(row["status"]),
        effect=Effect(row["effect"]),
        reason=row["reason"],
        post_observation_id=row["post_observation_id"],
        created_at=row["created_at"],
        sent_at=row["sent_at"],
        finished_at=row["finished_at"],
        policy=_policy(row["policy"]),
        found_apps=None if row["found_apps"] is None else list(row["found_apps"]),
        intent=None if row["intent"] is None else dict(row["intent"]),
        reconciled_by=row["reconciled_by"],
        retry_of=row["retry_of"],
        purchase_intent_id=row["purchase_intent_id"],
        field_text=row["field_text"],
        field_info=None if row["field_info"] is None else dict(row["field_info"]),
        changes=row["changes"],
    )


async def insert_action(conn: AsyncConnection, action: Action) -> None:
    await execute(
        conn,
        """INSERT INTO actions (id, tenant_id, task_id, run_id, device_id, tool_call_id, epoch,
                   control_revision, observation_id, kind, input, target, expected_effect,
                   effects, digest, status, effect, reason, post_observation_id, policy, intent,
                   retry_of, purchase_intent_id, changes)
           VALUES (:id, :tenant_id, :task_id, :run_id, :device_id, :tool_call_id, :epoch,
                   :control_revision, :observation_id, :kind, CAST(:input AS jsonb), :target,
                   :expected_effect, :effects, :digest, :status, :effect, :reason,
                   :post_observation_id, CAST(:policy AS jsonb), CAST(:intent AS jsonb),
                   :retry_of, :purchase_intent_id, :changes)""",
        id=action.id,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        run_id=action.run_id,
        device_id=action.device_id,
        tool_call_id=action.tool_call_id,
        epoch=action.epoch,
        control_revision=action.control_revision,
        observation_id=action.observation_id,
        kind=action.kind,
        input=jsonb(action.input),
        target=action.target,
        expected_effect=action.expected_effect,
        effects=action.effects,
        digest=action.digest,
        status=action.status.value,
        effect=action.effect.value,
        reason=action.reason,
        post_observation_id=action.post_observation_id,
        policy=jsonb(policy_json(action.policy)),
        intent=jsonb(action.intent),
        retry_of=action.retry_of,
        purchase_intent_id=action.purchase_intent_id,
        changes=action.changes,
    )


async def get_action(
    conn: AsyncConnection, action_id: UUID, *, lock: bool = False
) -> Action | None:
    row = await fetch_one(
        conn,
        f"SELECT {_ACTION} FROM actions WHERE id = :id" + (" FOR UPDATE" if lock else ""),
        id=action_id,
    )
    return _action(row) if row else None


async def task_actions(conn: AsyncConnection, task_id: UUID) -> list[Action]:
    rows = await fetch(
        conn, f"SELECT {_ACTION} FROM actions WHERE task_id = :t ORDER BY created_at", t=task_id
    )
    return [_action(r) for r in rows]


async def action_by_post_observation(conn: AsyncConnection, obs_id: UUID) -> UUID | None:
    row = await fetch_one(conn, "SELECT id FROM actions WHERE post_observation_id = :o", o=obs_id)
    return row["id"] if row else None


async def device_state(conn: AsyncConnection, device_id: UUID) -> RowMapping:
    """What admission needs to know about recent input on a device."""
    row = await fetch_one(
        conn,
        """SELECT
             EXISTS (SELECT 1 FROM actions WHERE device_id = :d AND status IN ('queued', 'sent'))
               AS busy,
             EXISTS (SELECT 1 FROM actions WHERE device_id = :d AND effect = 'unknown')
               AS unknown,
             (SELECT MAX(COALESCE(finished_at, sent_at)) FROM actions
               WHERE device_id = :d AND sent_at IS NOT NULL) AS last_input_at""",
        d=device_id,
    )
    assert row is not None
    return row


async def actions_with_status(
    conn: AsyncConnection, device_ids: list[UUID], status: ActionStatus
) -> list[Action]:
    rows = await fetch(
        conn,
        f"""SELECT {_ACTION} FROM actions
             WHERE device_id = ANY(:d) AND status = :s ORDER BY created_at""",
        d=device_ids,
        s=status.value,
    )
    return [_action(r) for r in rows]


async def update_action(conn: AsyncConnection, action_id: UUID, **fields: Any) -> None:
    allowed = {
        "status",
        "effect",
        "reason",
        "sent_at",
        "finished_at",
        "reconciled_by",
        "reconciled_with",
        "policy",
        "found_apps",
        "field_text",
        "field_info",
    }
    assert set(fields) <= allowed, set(fields) - allowed
    values: dict[str, Any] = {
        k: (v.value if isinstance(v, ActionStatus | Effect) else v) for k, v in fields.items()
    }
    if "policy" in values:
        values["policy"] = jsonb(policy_json(values["policy"]))
    if "found_apps" in values:
        values["found_apps"] = jsonb(values["found_apps"])
    if "field_info" in values:
        values["field_info"] = jsonb(values["field_info"])
    sets = ", ".join(
        f"{k} = CAST(:{k} AS jsonb)"
        if k in ("policy", "found_apps", "field_info")
        else f"{k} = :{k}"
        for k in values
    )
    await execute(conn, f"UPDATE actions SET {sets} WHERE id = :id", id=action_id, **values)


async def cancel_unsent(
    conn: AsyncConnection, task_id: UUID, reason: str, run_id: UUID | None = None
) -> list[UUID]:
    """The task's actions not sent yet (only one run's, with ``run_id``) are cancelled."""
    rows = await fetch(
        conn,
        """UPDATE actions SET status = 'cancelled', reason = :r
            WHERE task_id = :t AND status IN ('awaiting_approval', 'queued')
              AND (CAST(:run AS uuid) IS NULL OR run_id = :run) RETURNING id""",
        t=task_id,
        r=reason,
        run=run_id,
    )
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- approvals

_APPROVAL = """action_id, tenant_id, digest, requested_at, expires_at, decision, decided_by,
               decided_at, used_at, grant_id"""


def _approval(row: RowMapping) -> Approval:
    return Approval(**dict(row))


async def insert_approval(
    conn: AsyncConnection, *, action_id: UUID, tenant_id: UUID, digest: str, expires_at: datetime
) -> None:
    await execute(
        conn,
        """INSERT INTO approvals (action_id, tenant_id, digest, expires_at)
           VALUES (:a, :t, :d, :e)""",
        a=action_id,
        t=tenant_id,
        d=digest,
        e=expires_at,
    )


async def get_approval(
    conn: AsyncConnection, action_id: UUID, *, lock: bool = False
) -> Approval | None:
    row = await fetch_one(
        conn,
        f"SELECT {_APPROVAL} FROM approvals WHERE action_id = :a" + (" FOR UPDATE" if lock else ""),
        a=action_id,
    )
    return _approval(row) if row else None


async def approvals_for(conn: AsyncConnection, action_ids: list[UUID]) -> dict[UUID, Approval]:
    rows = await fetch(
        conn, f"SELECT {_APPROVAL} FROM approvals WHERE action_id = ANY(:a)", a=action_ids
    )
    return {r["action_id"]: _approval(r) for r in rows}


async def decide_approval(
    conn: AsyncConnection,
    action_id: UUID,
    *,
    decision: str,
    user_id: UUID,
    via: str,
    client: str | None,
) -> None:
    await execute(
        conn,
        """UPDATE approvals SET decision = :d, decided_by = :u, decided_at = now(),
                  decided_via = :via, decided_client = :client
            WHERE action_id = :a AND decision IS NULL""",
        a=action_id,
        d=decision,
        u=user_id,
        via=via,
        client=client,
    )


async def use_approval(conn: AsyncConnection, action_id: UUID) -> bool:
    count = await execute(
        conn,
        """UPDATE approvals SET used_at = now()
            WHERE action_id = :a AND decision = 'approve' AND used_at IS NULL""",
        a=action_id,
    )
    return count == 1


async def approve_by_grant(
    conn: AsyncConnection,
    *,
    action_id: UUID,
    tenant_id: UUID,
    digest: str,
    expires_at: datetime,
    grant: Grant,
) -> None:
    """An approval the grant gives, decided on behalf of the person who granted it."""
    await execute(
        conn,
        """INSERT INTO approvals (action_id, tenant_id, digest, expires_at, decision,
                                  decided_by, decided_at, decided_via, grant_id)
           VALUES (:a, :t, :d, :e, 'approve', :u, now(), 'grant', :g)""",
        a=action_id,
        t=tenant_id,
        d=digest,
        e=expires_at,
        u=grant.created_by,
        g=grant.id,
    )


async def reopen_approval(conn: AsyncConnection, action_id: UUID) -> bool:
    """Takes back an unused approval a grant gave: the person decides the input again,
    before the approval's original expiry."""
    count = await execute(
        conn,
        """UPDATE approvals SET decision = NULL, decided_by = NULL, decided_at = NULL,
                  decided_via = NULL, grant_id = NULL
            WHERE action_id = :a AND grant_id IS NOT NULL AND used_at IS NULL""",
        a=action_id,
    )
    return count == 1


async def unused_grant_approvals(conn: AsyncConnection, grant_id: UUID) -> list[UUID]:
    rows = await fetch(
        conn,
        "SELECT action_id FROM approvals WHERE grant_id = :g AND used_at IS NULL",
        g=grant_id,
    )
    return [r["action_id"] for r in rows]


# ---------------------------------------------------------------- grants

_GRANT = """id, tenant_id, task_id, created_from, device_id, app, effects, terms,
            control_revision, max_uses, uses, created_by, created_at, expires_at, revoked_at,
            revoked_by"""


def _grant(row: RowMapping) -> Grant:
    return Grant(**{**dict(row), "effects": list(row["effects"]), "terms": dict(row["terms"])})


async def insert_grant(conn: AsyncConnection, grant: Grant) -> None:
    await execute(
        conn,
        """INSERT INTO grants (id, tenant_id, task_id, created_from, device_id, app, effects,
                               terms, control_revision, max_uses, uses, created_by,
                               created_at, expires_at)
           VALUES (:id, :tenant_id, :task_id, :created_from, :device_id, :app, :effects,
                   CAST(:terms AS jsonb), :control_revision, :max_uses, :uses, :created_by,
                   :created_at, :expires_at)""",
        id=grant.id,
        tenant_id=grant.tenant_id,
        task_id=grant.task_id,
        created_from=grant.created_from,
        device_id=grant.device_id,
        app=grant.app,
        effects=grant.effects,
        terms=jsonb(grant.terms),
        control_revision=grant.control_revision,
        max_uses=grant.max_uses,
        uses=grant.uses,
        created_by=grant.created_by,
        created_at=grant.created_at,
        expires_at=grant.expires_at,
    )


async def get_grant(conn: AsyncConnection, grant_id: UUID, *, lock: bool = False) -> Grant | None:
    row = await fetch_one(
        conn,
        f"SELECT {_GRANT} FROM grants WHERE id = :g" + (" FOR UPDATE" if lock else ""),
        g=grant_id,
    )
    return _grant(row) if row else None


async def task_grants(
    conn: AsyncConnection, task_id: UUID, *, open_only: bool = False, lock: bool = False
) -> list[Grant]:
    """The task's grants, oldest first; ``open_only`` leaves out revoked ones."""
    rows = await fetch(
        conn,
        f"SELECT {_GRANT} FROM grants WHERE task_id = :t"
        + (" AND revoked_at IS NULL" if open_only else "")
        + " ORDER BY created_at, id"
        + (" FOR UPDATE" if lock else ""),
        t=task_id,
    )
    return [_grant(r) for r in rows]


async def use_grant(conn: AsyncConnection, grant_id: UUID) -> None:
    await execute(conn, "UPDATE grants SET uses = uses + 1 WHERE id = :g", g=grant_id)


async def revoke_grant(conn: AsyncConnection, grant_id: UUID, user_id: UUID) -> bool:
    count = await execute(
        conn,
        """UPDATE grants SET revoked_at = now(), revoked_by = :u
            WHERE id = :g AND revoked_at IS NULL""",
        g=grant_id,
        u=user_id,
    )
    return count == 1
