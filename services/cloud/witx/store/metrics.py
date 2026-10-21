"""The numbers behind /metrics , read from the database so that every API
instance reports the same values. Windows are the last 24 hours. Runs under one tenant's
row-level security."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.store.db import fetch, fetch_one

WINDOW = "interval '24 hours'"


@dataclass(frozen=True)
class Sample:
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


async def _counts(
    conn: AsyncConnection, name: str, sql: str, labels: list[str], **params: object
) -> list[Sample]:
    rows = await fetch(conn, sql, **params)
    return [Sample(name, float(r["n"]), {k: str(r[k]) for k in labels}) for r in rows]


async def _quantiles(conn: AsyncConnection, name: str, seconds_sql: str) -> list[Sample]:
    """p50 and p90 of a duration in seconds (no samples when nothing happened)."""
    row = await fetch_one(
        conn,
        f"""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY s) AS p90
              FROM ({seconds_sql}) AS d(s)""",
    )
    if row is None or row["p50"] is None:
        return []
    return [
        Sample(name, float(row["p50"]), {"quantile": "0.5"}),
        Sample(name, float(row["p90"]), {"quantile": "0.9"}),
    ]


async def tenant_samples(conn: AsyncConnection, now: datetime) -> list[Sample]:
    samples: list[Sample] = []
    samples += await _counts(
        conn,
        "witx_tasks",
        "SELECT status, count(*) AS n FROM tasks WHERE deleted_at IS NULL GROUP BY status",
        ["status"],
    )
    samples += await _counts(
        conn,
        "witx_tasks_finished_24h",
        f"""SELECT status, count(*) AS n FROM tasks
             WHERE status IN ('completed', 'failed', 'stopped') AND updated_at > now() - {WINDOW}
             GROUP BY status""",
        ["status"],
    )
    samples += await _counts(
        conn,
        "witx_actions_24h",
        f"""SELECT kind, effect, count(*) AS n FROM actions
             WHERE created_at > now() - {WINDOW} GROUP BY kind, effect""",
        ["kind", "effect"],
    )
    open_unknown = await fetch_one(
        conn, "SELECT count(*) AS n FROM actions WHERE effect = 'unknown'"
    )
    samples.append(
        Sample("witx_actions_unknown_open", float(open_unknown["n"] if open_unknown else 0))
    )
    samples += await _counts(
        conn,
        "witx_approvals_24h",
        f"""SELECT coalesce(decision, 'undecided') AS decision, count(*) AS n FROM approvals
             WHERE requested_at > now() - {WINDOW} GROUP BY 1""",
        ["decision"],
    )
    takeovers = await fetch_one(
        conn,
        f"""SELECT count(*) AS n FROM events
             WHERE type = 'takeover.started' AND created_at > now() - {WINDOW}""",
    )
    samples.append(Sample("witx_takeovers_24h", float(takeovers["n"] if takeovers else 0)))
    samples += await _counts(
        conn,
        "witx_steps_24h",
        f"""SELECT status, count(*) AS n FROM steps
             WHERE started_at > now() - {WINDOW} GROUP BY status""",
        ["status"],
    )
    samples += await _quantiles(
        conn,
        "witx_step_seconds_24h",
        f"""SELECT extract(epoch FROM finished_at - started_at) FROM steps
             WHERE status = 'done' AND finished_at > now() - {WINDOW}""",
    )
    samples += await _quantiles(
        conn,
        "witx_risk_check_seconds_24h",
        f"""SELECT (risk_call->>'duration_ms')::float / 1000 FROM steps
             WHERE risk_call IS NOT NULL AND finished_at > now() - {WINDOW}""",
    )
    samples += await _quantiles(
        conn,
        "witx_action_seconds_24h",
        f"""SELECT extract(epoch FROM finished_at - sent_at) FROM actions
             WHERE sent_at IS NOT NULL AND finished_at > now() - {WINDOW}""",
    )
    tokens = await fetch_one(
        conn,
        f"""SELECT coalesce(sum((usage->>'input_tokens')::bigint), 0)
                     + coalesce(sum((risk_call->'usage'->>'input_tokens')::bigint), 0) AS input,
                   coalesce(sum((usage->>'output_tokens')::bigint), 0)
                     + coalesce(sum((risk_call->'usage'->>'output_tokens')::bigint), 0) AS output
              FROM steps WHERE started_at > now() - {WINDOW}""",
    )
    if tokens is not None:
        for direction in ("input", "output"):
            samples.append(
                Sample("witx_model_tokens_24h", float(tokens[direction]), {"direction": direction})
            )
    samples += await _counts(
        conn,
        "witx_observations_24h",
        f"""SELECT status, count(*) AS n FROM observations
             WHERE requested_at > now() - {WINDOW} GROUP BY status""",
        ["status"],
    )
    samples += await _counts(
        conn,
        "witx_runs",
        """SELECT status, coalesce(wait_reason, '') AS wait_reason, count(*) AS n FROM runs
            WHERE status IN ('runnable', 'running', 'waiting') GROUP BY 1, 2""",
        ["status", "wait_reason"],
    )
    queued = await fetch_one(
        conn,
        """SELECT coalesce(extract(epoch FROM max(:now - updated_at)), 0) AS s FROM runs
            WHERE status = 'runnable'""",
        now=now,
    )
    samples.append(Sample("witx_run_queue_seconds", float(queued["s"] if queued else 0)))
    samples += await _counts(
        conn,
        "witx_devices",
        """SELECT platform, online::text AS online, count(*) AS n FROM devices
            WHERE gateway_id NOT IN (SELECT id FROM gateways WHERE revoked_at IS NOT NULL)
            GROUP BY 1, 2""",
        ["platform", "online"],
    )
    sweep = await fetch_one(
        conn,
        """SELECT scanned, deleted, skipped_held, skipped_unrecognized, failed, error,
                  extract(epoch FROM :now - finished_at) AS age, duration_ms
             FROM screenshot_sweeps""",
        now=now,
    )
    if sweep is not None:
        for result in ("scanned", "deleted", "skipped_held", "skipped_unrecognized", "failed"):
            samples.append(
                Sample("witx_screenshot_sweep_objects", float(sweep[result]), {"result": result})
            )
        outcome = {"outcome": "error" if sweep["error"] else "ok"}
        samples.append(Sample("witx_screenshot_sweep_age_seconds", float(sweep["age"]), outcome))
        samples.append(
            Sample("witx_screenshot_sweep_seconds", sweep["duration_ms"] / 1000, outcome)
        )
    return samples
