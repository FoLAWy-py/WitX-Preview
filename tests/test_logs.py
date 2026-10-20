"""The log format shared by the cloud and the Gateway ."""

import io
import logging
from collections.abc import Callable

from witx_contracts import logs

log = logging.getLogger("witx.test")


def test_a_log_line_carries_the_ids_of_its_context(
    log_lines: Callable[[], list[dict[str, object]]],
) -> None:
    with logs.log_context(tenant_id="t1", run_id="r1"):
        logs.bind(step_id="s1")
        log.info("model call", extra={"duration_ms": 812})
    log.info("outside")
    inside, outside = log_lines()
    assert inside["msg"] == "model call" and inside["level"] == "info"
    assert inside["service"] == "test"
    assert (inside["tenant_id"], inside["run_id"], inside["step_id"]) == ("t1", "r1", "s1")
    assert inside["duration_ms"] == 812
    assert "run_id" not in outside and "step_id" not in outside


def test_credentials_typed_text_and_personal_numbers_never_reach_a_log(
    log_lines: Callable[[], list[dict[str, object]]],
) -> None:
    secrets = [
        "Bearer abc.def",
        "wgw_AbCdEf123",
        "code=one-time&state=s",
        "ann@example.com",
        "+86 138 0013 8000",
    ]
    log.warning(
        "got %s",
        " ".join(secrets),
        extra={"token": "t0k", "text": "my password", "x_csrf_token": "c", "input_tokens": 5},
    )
    try:
        raise ValueError("Authorization: Bearer leaked")
    except ValueError:
        log.exception("failed")
    assert log_lines()[0]["input_tokens"] == 5  # a count, not a secret
    output = str(log_lines())
    for secret in [*secrets, "t0k", "my password", "leaked"]:
        assert secret not in output.replace("[redacted]", ""), secret
    assert "2026-10-11" in logs.redact("at 2026-10-11 03:13:37")  # dates are kept


def test_the_text_format_shows_the_same_fields() -> None:
    stream = io.StringIO()
    handler = logs.handler("api", stream, text=True)
    record = log.makeRecord("witx.test", logging.INFO, "f", 1, "request", None, None)
    record.route = "/api/v1/tasks/{task_id}"
    with logs.log_context(task_id="t1"):
        handler.handle(record)
    line = stream.getvalue()
    assert "INFO" in line and "request" in line
    assert "task_id=t1" in line and "route=/api/v1/tasks/{task_id}" in line
