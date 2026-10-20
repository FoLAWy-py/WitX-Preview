"""How WitX processes log : one JSON object per line, carrying the IDs that
tie it to a tenant, task, run, step, action or observation, with secrets removed.

Shared by the cloud and the Gateway so that both write the same fields.

    configure("api")                         # once, at process start
    with log_context(run_id=..., task_id=...):
        log.info("model call", extra={"duration_ms": 812})

``WITX_LOG_FORMAT=text`` prints the same fields for people; ``WITX_LOG_LEVEL`` sets the
level (default INFO).
"""

import json
import logging
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

_context: ContextVar[Mapping[str, str]] = ContextVar("witx_log_context")

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "witx"}

# Values under these keys never reach a log, whatever they hold.
# Whole name segments: `access_token` is secret, `input_tokens` is a count.
_SECRET_KEYS = re.compile(
    r"(?:^|_)(?:authorization|cookie|token|secret|password|credential|csrf)(?:_|$)", re.I
)
_SECRET_NAMES = {"code", "text", "value"}  # typed text, picker values, one-time codes
_SECRETS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]+"), r"\1 [redacted]"),
    (re.compile(r"\bwgw_[\w-]+"), "wgw_[redacted]"),
    (re.compile(r"(?i)\b(code|state|token|secret|password)=[^&\s]+"), r"\1=[redacted]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
]
_NUMBER = re.compile(r"(?<![\w-])\+?\d[\d ()-]{7,}\d(?![\w-])")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _number(match: re.Match[str]) -> str:
    found = match.group()
    return found if _DATE.match(found) else "[number]"  # dates are not phone numbers


def redact(text: str) -> str:
    """Removes credentials, e-mail addresses and phone-like numbers from free text."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return _NUMBER.sub(_number, text)


def _clean(key: str, value: Any) -> Any:
    if _SECRET_KEYS.search(key) or key in _SECRET_NAMES:
        return "[redacted]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


def _merged(ids: Mapping[str, object]) -> Mapping[str, str]:
    return {**_context.get({}), **{k: str(v) for k, v in ids.items() if v is not None}}


@contextmanager
def log_context(**ids: object) -> Iterator[None]:
    """Adds IDs to every record logged inside the block (and in tasks it starts)."""
    token = _context.set(_merged(ids))
    try:
        yield
    finally:
        _context.reset(token)


def bind(**ids: object) -> None:
    """Adds IDs for the rest of the current context, e.g. once a request knows its tenant.
    The enclosing ``log_context`` removes them again when it ends."""
    _context.set(_merged(ids))


_ID_FIELDS = (
    "device_id", "action_id", "observation_id", "stop_id", "input_id", "request_id",
    "takeover_id",
)  # fmt: skip


def ids(message: object) -> dict[str, object]:
    """The correlation IDs a message carries, for ``log_context(**ids(message))``."""
    return {k: getattr(message, k) for k in _ID_FIELDS if getattr(message, k, None)}


class _Stamp(logging.Filter):
    """Copies the context onto the record when it is logged, before any queueing."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.witx = {"service": self.service, **_context.get({})}
        return True


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = dict(getattr(record, "witx", {}))
    for key, value in vars(record).items():
        if key not in _STANDARD and not key.startswith("_"):
            fields[key] = _clean(key, value)
    return fields


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
            **_fields(record),
        }
        if record.exc_info:
            line["error"] = redact(self.formatException(record.exc_info))
        return json.dumps(line, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        when = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        fields = " ".join(f"{k}={v}" for k, v in _fields(record).items() if k != "service")
        line = f"{when} {record.levelname:<7} {record.name}: {redact(record.getMessage())}"
        if fields:
            line += f"  {fields}"
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def handler(service: str, stream: TextIO, *, text: bool = False) -> logging.Handler:
    """A handler that writes WitX log lines for ``service`` to ``stream``."""
    out = logging.StreamHandler(stream)
    out.addFilter(_Stamp(service))
    out.setFormatter(TextFormatter() if text else JsonFormatter())
    return out


def configure(service: str) -> None:
    """Sends every log record to stderr in the configured format."""
    text = os.environ.get("WITX_LOG_FORMAT", "json").lower() == "text"
    root = logging.getLogger()
    root.handlers[:] = [handler(service, sys.stderr, text=text)]
    root.setLevel(os.environ.get("WITX_LOG_LEVEL", "INFO").upper())
    # Their request lines carry full URLs; ours carry the route instead.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
