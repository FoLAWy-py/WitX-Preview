"""Private object storage for screenshots (filesystem for development, S3 for deployment)."""

import asyncio
import hashlib
import hmac
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import httpx

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-/.]{0,254}$")


class ObjectNotFound(Exception):
    pass


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None:
        """Removes the object; deleting a missing one is not an error (retries)."""
        ...

    async def list(self, prefix: str) -> list[str]:
        """The keys that start with ``prefix``, in key order."""
        ...


def _check_key(key: str) -> str:
    if not _KEY.match(key) or ".." in key.split("/"):
        raise ValueError(f"invalid object key: {key!r}")
    return key


class LocalObjectStore:
    """Files under one directory, written atomically and readable only by this user."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._write, self._path(key), data)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError:
            raise ObjectNotFound(key) from None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def list(self, prefix: str) -> list[str]:
        def walk() -> list[str]:
            keys = (str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())
            return sorted(k for k in keys if k.startswith(prefix))

        return await asyncio.to_thread(walk)


class S3ObjectStore:
    """Minimal S3-compatible client (path-style URLs, AWS Signature V4)."""

    def __init__(
        self, *, endpoint: str, bucket: str, region: str, access_key: str, secret_key: str
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.client = httpx.AsyncClient(timeout=30)

    def _signed_headers(
        self, method: str, path: str, body: bytes, extra: dict[str, str], query: str = ""
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        host = urlsplit(self.endpoint).netloc
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            **{k.lower(): v for k, v in extra.items()},
        }
        names = sorted(headers)
        canonical = "\n".join(
            [
                method,
                path,
                query,
                "".join(f"{n}:{headers[n].strip()}\n" for n in names),
                ";".join(names),
                payload_hash,
            ]
        )
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
        )
        key = ("AWS4" + self.secret_key).encode()
        for part in (day, self.region, "s3", "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        )
        del headers["host"]
        return headers

    def _path(self, key: str) -> str:
        return f"/{quote(self.bucket)}/{quote(_check_key(key))}"

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        headers = self._signed_headers("PUT", path, data, {"content-type": content_type})
        response = await self.client.put(self.endpoint + path, content=data, headers=headers)
        response.raise_for_status()

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        headers = self._signed_headers("GET", path, b"", {})
        response = await self.client.get(self.endpoint + path, headers=headers)
        if response.status_code == 404:
            raise ObjectNotFound(key)
        response.raise_for_status()
        return response.content

    async def delete(self, key: str) -> None:
        path = self._path(key)
        headers = self._signed_headers("DELETE", path, b"", {})
        response = await self.client.delete(self.endpoint + path, headers=headers)
        if response.status_code != 404:  # S3 answers 204 either way; others may say 404
            response.raise_for_status()

    async def list(self, prefix: str) -> list[str]:
        path = f"/{quote(self.bucket)}"
        keys: list[str] = []
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": prefix} | (
                {"continuation-token": token} if token else {}
            )
            query = "&".join(
                f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
                for k, v in sorted(params.items())
            )
            headers = self._signed_headers("GET", path, b"", {}, query)
            response = await self.client.get(f"{self.endpoint}{path}?{query}", headers=headers)
            response.raise_for_status()
            page, token = _list_page(response.content)
            keys += page
            if token is None:
                return sorted(keys)


def _list_page(xml: bytes) -> tuple[list[str], str | None]:
    """The keys of one ListObjectsV2 page and the token for the next (None: last)."""
    root = ElementTree.fromstring(xml)
    ns = {"s3": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    tag = "s3:" if ns else ""
    keys = [e.text or "" for e in root.iterfind(f".//{tag}Contents/{tag}Key", ns)]
    truncated = root.findtext(f"{tag}IsTruncated", default="false", namespaces=ns) == "true"
    return keys, root.findtext(f"{tag}NextContinuationToken", namespaces=ns) if truncated else None


def object_store_from_url(url: str) -> ObjectStore:
    """``file:///abs/path`` or ``s3://bucket?endpoint=https://...&region=...``.

    S3 credentials come from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.
    """
    parts = urlsplit(url)
    if parts.scheme == "file":
        return LocalObjectStore(Path(parts.path))
    if parts.scheme == "s3":
        query = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
        return S3ObjectStore(
            endpoint=query.get("endpoint", "https://s3.amazonaws.com"),
            bucket=parts.netloc,
            region=query.get("region", "us-east-1"),
            access_key=os.environ["AWS_ACCESS_KEY_ID"],
            secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        )
    raise ValueError(f"unsupported object store URL: {url}")
