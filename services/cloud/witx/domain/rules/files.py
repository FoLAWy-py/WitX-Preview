"""Files the user gives a task, and the links a phone downloads them by."""

import base64
import hashlib
import hmac
import struct
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

from witx.domain.rules.common import Rejected

# The largest file a person may give a task.
MAX_FILE_BYTES = 100 * 1024 * 1024
# The most files one message may carry.
MAX_FILES_PER_MESSAGE = 10
# How long a download link works: long enough for the phone to fetch the file and resume
# it after a drop, short enough that a link seen later is useless.
LINK_TTL = timedelta(minutes=15)
# A file uploaded but never attached to a task is purged after this.
UNATTACHED_TTL = timedelta(days=1)


def check_file_size(size: int) -> None:
    """A file the person gives a task is at most MAX_FILE_BYTES."""
    if size > MAX_FILE_BYTES:
        raise Rejected("file_too_large", f"at most {MAX_FILE_BYTES // 2**20} MiB")


def file_name(name: str) -> str:
    """The name a file is kept and served under: the last part of what the browser sent,
    without control characters, no hidden name, at most 255 characters."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C").strip()
    name = name.lstrip(".").strip()[:255]
    if not name:
        raise Rejected("invalid_arguments", "the file needs a name")
    return name


def byte_size(n: int) -> str:
    """A size as people read it: 512 B, 2.4 KB, 13.0 MB (decimal units, as phones show)."""
    if n < 1000:
        return f"{n} B"
    value, unit = n / 1000, "KB"
    if value >= 1000:
        value, unit = value / 1000, "MB"
    return f"{value:.1f} {unit}"


def media_type(header: str | None) -> str:
    """The file's type as the browser named it, if it looks like one; else plain bytes.
    It is only a label: nothing here opens the file by it."""
    value = (header or "").split(";")[0].strip().lower()
    head, sep, tail = value.partition("/")
    ok = sep and head and tail and len(value) <= 127
    if ok and all(c.isalnum() or c in "!#$&-^_.+" for c in head + tail):
        return value
    return "application/octet-stream"


def content_disposition(name: str) -> str:
    """``attachment`` with the name for any client: an ASCII stand-in, and the real name
    in UTF-8 (RFC 6266), which the phone's downloader prefers."""
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


@dataclass(frozen=True)
class Link:
    """What a download link says: which tenant's file, until when. Not yet checked."""

    tenant_id: UUID
    file_id: UUID
    expires: datetime
    mac: bytes


_CLAIM = struct.Struct(">16s16sQ")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, claim: bytes) -> bytes:
    return hmac.new(key, b"witx-download\0" + claim, hashlib.sha256).digest()


def link_token(key: bytes, tenant_id: UUID, file_id: UUID, expires: datetime) -> str:
    """A token for the download path: the tenant, the file and the expiry, signed with
    the tenant's key. Only URL-safe characters, no ``/``."""
    claim = _CLAIM.pack(tenant_id.bytes, file_id.bytes, int(expires.timestamp()))
    return f"{_b64(claim)}.{_b64(_mac(key, claim))}"


def read_link(token: str) -> Link | None:
    """What a token claims, or None if it is not one of ours. Check it with link_valid."""
    try:
        claim_text, mac_text = token.split(".")
        claim, mac = _unb64(claim_text), _unb64(mac_text)
        tenant, file, expires = _CLAIM.unpack(claim)
    except (ValueError, struct.error):
        return None
    return Link(UUID(bytes=tenant), UUID(bytes=file), datetime.fromtimestamp(expires, UTC), mac)


def link_valid(link: Link, key: bytes, now: datetime) -> bool:
    """The tenant's key signed it, and it has not expired."""
    claim = _CLAIM.pack(link.tenant_id.bytes, link.file_id.bytes, int(link.expires.timestamp()))
    return hmac.compare_digest(_mac(key, claim), link.mac) and now < link.expires


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The bytes a ``Range`` header asks for, first and last inclusive; None for the
    whole file (no header, or one this server does not serve in part, such as several
    ranges). Raises ``Rejected("range_not_satisfiable")`` for a range past the end."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, sep, last = header.removeprefix("bytes=").strip().partition("-")
    if not sep or not (first.isdigit() or last.isdigit()):
        return None
    if not first:  # the last N bytes
        n = int(last)
        if n == 0 or size == 0:
            raise Rejected("range_not_satisfiable")
        return max(0, size - n), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last.isdigit() else size - 1
    if start >= size or (last.isdigit() and int(last) < start):
        raise Rejected("range_not_satisfiable")
    return start, end
