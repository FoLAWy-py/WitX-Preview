"""Prometheus text exposition of the operational numbers .

Everything is computed from the database at scrape time, tenant by tenant under row-level
security, and labelled with the tenant's name: every API instance reports the same
values and no process keeps counters that a restart would reset.
"""

from datetime import UTC, datetime

from witx.store import identity, metrics
from witx.store.db import Database

HELP = {
    "witx_tasks": "Tasks by status (deleted ones excluded).",
    "witx_tasks_finished_24h": "Tasks that ended in the last 24 h, by final status.",
    "witx_actions_24h": "Inputs created in the last 24 h, by kind and effect.",
    "witx_actions_unknown_open": "Inputs whose effect is unknown and not yet reconciled.",
    "witx_approvals_24h": "Approvals requested in the last 24 h, by decision.",
    "witx_takeovers_24h": "Takeovers started in the last 24 h.",
    "witx_steps_24h": "Agent steps (model calls) started in the last 24 h, by status.",
    "witx_step_seconds_24h": "Duration of an agent step (model call and risk check).",
    "witx_risk_check_seconds_24h": "Duration of the independent risk check.",
    "witx_action_seconds_24h": "From sending an input to the Gateway to its result.",
    "witx_model_tokens_24h": "Provider-reported tokens in the last 24 h.",
    "witx_observations_24h": "Screenshots requested in the last 24 h, by status.",
    "witx_runs": "Unfinished runs by status and wait reason.",
    "witx_run_queue_seconds": "Age of the oldest run waiting for a worker.",
    "witx_devices": "Devices by platform and connection.",
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


async def render(db: Database) -> str:
    now = datetime.now(UTC)
    by_name: dict[str, list[str]] = {name: [] for name in HELP}
    async with db.system() as conn:
        tenants = list(await identity.tenants(conn))
    for tenant in tenants:
        async with db.tenant(tenant["id"]) as conn:
            samples = await metrics.tenant_samples(conn, now)
        for sample in samples:
            labels = {"tenant": tenant["name"], **sample.labels}
            text = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
            value = int(sample.value) if sample.value.is_integer() else round(sample.value, 6)
            by_name[sample.name].append(f"{sample.name}{{{text}}} {value}")
    lines = []
    for name, help_text in HELP.items():
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", *by_name[name]]
    return "\n".join(lines) + "\n"
