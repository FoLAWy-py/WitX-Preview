"""Authentication for the Web API: bearer tokens for programs, and the OIDC sign-in that
gives browsers a session cookie."""

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any, ClassVar, Protocol
from urllib.parse import quote, urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet


class Authenticator(Protocol):
    async def subject(self, token: str) -> str | None:
        """The verified user subject for a token, or None if it is not valid."""
        ...


class DevAuthenticator:
    """Fixed tokens from configuration. For local development and tests only."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    async def subject(self, token: str) -> str | None:
        for known, subject in self._tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                return subject
        return None


class OidcAuthenticator:
    """Verifies OIDC access tokens (JWT) against the issuer's published keys."""

    ALGORITHMS: ClassVar[list[str]] = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]

    def __init__(self, *, issuer: str, audience: str, jwks_url: str, cache_seconds: int = 600):
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self.cache_seconds = cache_seconds
        self._keys: KeySet | None = None
        self._fetched_at = 0.0
        self._claims = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": issuer},
            aud={"essential": True, "value": audience},
            sub={"essential": True},
            exp={"essential": True},
        )

    async def _key_set(self, refresh: bool) -> KeySet:
        stale = time.monotonic() - self._fetched_at > self.cache_seconds
        if self._keys is None or refresh or stale:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
            self._keys = KeySet.import_key_set(response.json())
            self._fetched_at = time.monotonic()
        return self._keys

    async def subject(self, token: str) -> str | None:
        for refresh in (False, True):  # retry once with fresh keys (key rotation)
            try:
                decoded = jwt.decode(
                    token, await self._key_set(refresh), algorithms=self.ALGORITHMS
                )
                self._claims.validate(decoded.claims)
            except (JoseError, ValueError):
                continue
            subject = decoded.claims.get("sub")
            return str(subject) if subject else None
        return None


class LoginFailed(Exception):
    """The sign-in could not be completed (bad state, rejected code, invalid ID token)."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _derive(secret: str, purpose: str) -> str:
    return _b64url(hashlib.sha256(f"{purpose}:{secret}".encode()).digest())


class OidcLogin:
    """The Web app's sign-in : authorization code + PKCE, run by the backend
    as a confidential client.

    Nothing is stored while the user is at the provider: a random value travels in a
    short-lived HttpOnly cookie, and the state, the nonce and the PKCE verifier are
    derived from it. Without that cookie nobody can produce a matching state, so a
    sign-in started by another site is refused.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
        audience: str | None = None,
        scope: str = "openid profile",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.audience = audience
        self.scope = scope
        self._http = httpx.AsyncClient(timeout=10, transport=transport)
        self._discovery: dict[str, Any] | None = None

    async def _config(self) -> dict[str, Any]:
        if self._discovery is None:
            response = await self._http.get(f"{self.issuer}/.well-known/openid-configuration")
            response.raise_for_status()
            self._discovery = response.json()
        return self._discovery

    @staticmethod
    def new_secret() -> str:
        return secrets.token_urlsafe(48)  # 64 characters: a valid PKCE verifier

    async def authorization_url(self, secret: str) -> str:
        challenge = _b64url(hashlib.sha256(secret.encode()).digest())
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": _derive(secret, "state"),
            "nonce": _derive(secret, "nonce"),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self.audience:
            params["audience"] = self.audience
        endpoint = (await self._config())["authorization_endpoint"]
        return f"{endpoint}?{urlencode(params)}"

    async def subject(self, *, code: str, state: str, secret: str) -> str:
        """Redeems the code and returns the verified subject of the ID token."""
        if not hmac.compare_digest(state, _derive(secret, "state")):
            raise LoginFailed("state")
        config = await self._config()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": secret,
        }
        endpoint = config["token_endpoint"]
        if self.client_secret:  # client_secret_basic, values form-encoded (RFC 6749 2.3.1)
            basic = httpx.BasicAuth(
                quote(self.client_id, safe=""), quote(self.client_secret, safe="")
            )
            response = await self._http.post(endpoint, data=form, auth=basic)
        else:
            form["client_id"] = self.client_id
            response = await self._http.post(endpoint, data=form)
        if response.status_code != 200:
            raise LoginFailed("token_endpoint")
        id_token = response.json().get("id_token")
        if not isinstance(id_token, str):
            raise LoginFailed("no_id_token")
        keys_response = await self._http.get(config["jwks_uri"])
        keys_response.raise_for_status()
        claims = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": self.issuer},
            aud={"essential": True, "value": self.client_id},
            sub={"essential": True},
            exp={"essential": True},
            nonce={"essential": True, "value": _derive(secret, "nonce")},
        )
        try:
            decoded = jwt.decode(
                id_token,
                KeySet.import_key_set(keys_response.json()),
                algorithms=OidcAuthenticator.ALGORITHMS,
            )
            claims.validate(decoded.claims)
        except (JoseError, ValueError):
            raise LoginFailed("id_token") from None
        return str(decoded.claims["sub"])
