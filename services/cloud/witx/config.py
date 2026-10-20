"""Settings, read once from environment variables (see `.env.example`)."""

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from witx.domain.rules import LOWEST_EFFORT, REASONING_EFFORTS, efforts_from


def plugins_dir() -> Path:
    """``WITX_PLUGINS_DIR``: the installed plugins the agent's skills come from."""
    return Path(os.environ.get("WITX_PLUGINS_DIR") or Path.home() / ".witx" / "plugins")


def _reasoning_effort() -> str:
    """The deployment's default effort: the risk check's, and a new task's unless the
    person chooses. Never below LOWEST_EFFORT, whatever a model allows."""
    value = os.environ.get("WITX_REASONING_EFFORT") or LOWEST_EFFORT
    if value not in efforts_from(LOWEST_EFFORT):
        allowed = ", ".join(efforts_from(LOWEST_EFFORT))
        raise ValueError(f"WITX_REASONING_EFFORT must be one of {allowed}")
    return value


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


# Where a model runs: OpenAI's Responses API, or DeepInfra's Chat Completions.
PROVIDERS = ("openai", "deepinfra")


@dataclass(frozen=True)
class CatalogModel:
    name: str  # what tasks, people and the risk check use
    provider: str
    provider_model: str  # the provider's own ID
    lowest_effort: str = LOWEST_EFFORT  # the least a person may choose for it

    @property
    def efforts(self) -> tuple[str, ...]:
        return efforts_from(self.lowest_effort)


def parse_models(spec: str) -> dict[str, CatalogModel]:
    """``WITX_MODELS``: comma-separated ``name=provider[:provider model ID][@lowest
    effort]``, e.g. ``gpt-6-luna=openai,gpt-6.1-sol=openai@low,deepseek-v4.1-flash=
    deepinfra:deepseek-ai/DeepSeek-V4.1-Flash``. Without ``@`` a model starts at medium."""
    models = {}
    for entry in filter(None, (e.strip() for e in spec.split(","))):
        name, _, target = entry.partition("=")
        target, _, lowest = target.partition("@")
        provider, _, provider_model = target.partition(":")
        lowest = lowest or LOWEST_EFFORT
        if not name or provider not in PROVIDERS or lowest not in REASONING_EFFORTS:
            raise ValueError(f"WITX_MODELS: {entry!r} is not name=provider[:model][@effort]")
        models[name] = CatalogModel(name, provider, provider_model or name, lowest)
    return models


@dataclass(frozen=True)
class Limits:
    observation_max_age: timedelta = timedelta(seconds=120)
    observation_timeout: timedelta = timedelta(seconds=60)
    action_ttl: timedelta = timedelta(seconds=30)
    approval_ttl: timedelta = timedelta(minutes=10)
    max_steps_per_run: int = 200
    max_child_depth: int = 2
    max_children_per_run: int = 4
    model_retries: int = 5
    # Screenshots attached to each model request (newest first); None attaches all of
    # them. Every screenshot stays stored either way. One keeps each step
    # fast: older screenshots cost seconds per call and are mostly not cached.
    context_images: int | None = 1
    # Whether the agent may turn web search on for a task that needs it (use_web_search,
    # D27). Each task starts without it.
    web_search: bool = True
    risk_timeout: timedelta = timedelta(seconds=20)
    # Context compression: earlier steps are summarized once this many steps have
    # passed since the last summary, or once a step's input reaches this many tokens;
    # the newest steps stay word for word.
    compress_after_steps: int = 40
    compress_at_tokens: int = 25_000
    compress_keep_steps: int = 8


@dataclass(frozen=True)
class Settings:
    database_url: str
    object_store_url: str
    default_model: str = "gpt-6-luna"
    reasoning_effort: str = "medium"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    deepinfra_api_key: str | None = None
    # The models tasks may use; empty means only the default model, at OpenAI.
    models: dict[str, CatalogModel] = field(default_factory=dict)
    auth: str = "dev"  # "dev" or "oidc"
    dev_tokens: dict[str, str] = field(default_factory=dict)  # bearer token -> subject
    oidc_issuer: str | None = None
    oidc_audience: str | None = None  # of access tokens sent as Bearer (non-browser clients)
    oidc_jwks_url: str | None = None
    # The Web app's sign-in: the backend is a confidential client (secret + PKCE).
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    # Where users reach the Web app: OIDC redirects, Secure cookies, the allowed Origin.
    public_url: str = "http://127.0.0.1:5173"
    cors_origins: list[str] = field(default_factory=list)
    # Retention : screenshots of finished tasks, and what a deleted task leaves.
    screenshot_retention: timedelta = timedelta(days=30)
    audit_retention: timedelta = timedelta(days=90)
    metrics_token: str | None = None  # /metrics is off without it
    limits: Limits = field(default_factory=Limits)

    def catalog(self) -> dict[str, CatalogModel]:
        if self.models:
            return self.models
        return {self.default_model: CatalogModel(self.default_model, "openai", self.default_model)}

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        dev_tokens = {}
        for pair in filter(None, env.get("WITX_DEV_TOKENS", "").split(",")):
            token, _, subject = pair.partition("=")
            dev_tokens[token.strip()] = subject.strip()
        images = env.get("WITX_CONTEXT_IMAGES")
        settings = cls(
            database_url=env["WITX_DATABASE_URL"],
            object_store_url=env.get("WITX_OBJECT_STORE", "file:///tmp/witx-objects"),
            default_model=env.get("WITX_DEFAULT_MODEL", "gpt-6-luna"),
            reasoning_effort=_reasoning_effort(),
            openai_api_key=env.get("OPENAI_API_KEY"),
            openai_base_url=env.get("WITX_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            deepinfra_api_key=env.get("DEEPINFRA_API_KEY") or None,
            models=parse_models(env.get("WITX_MODELS", "")),
            auth=env.get("WITX_AUTH", "dev"),
            dev_tokens=dev_tokens,
            oidc_issuer=env.get("WITX_OIDC_ISSUER"),
            oidc_audience=env.get("WITX_OIDC_AUDIENCE"),
            oidc_jwks_url=env.get("WITX_OIDC_JWKS_URL"),
            oidc_client_id=env.get("WITX_OIDC_CLIENT_ID"),
            oidc_client_secret=env.get("WITX_OIDC_CLIENT_SECRET"),
            public_url=env.get("WITX_PUBLIC_URL", "http://127.0.0.1:5173").rstrip("/"),
            cors_origins=[o for o in env.get("WITX_CORS_ORIGINS", "").split(",") if o],
            screenshot_retention=timedelta(days=_int("WITX_SCREENSHOT_RETENTION_DAYS", 30)),
            audit_retention=timedelta(days=_int("WITX_AUDIT_RETENTION_DAYS", 90)),
            metrics_token=env.get("WITX_METRICS_TOKEN") or None,
            limits=Limits(
                observation_max_age=timedelta(seconds=_int("WITX_OBSERVATION_MAX_AGE", 120)),
                approval_ttl=timedelta(seconds=_int("WITX_APPROVAL_TTL", 600)),
                max_steps_per_run=_int("WITX_MAX_STEPS_PER_RUN", 200),
                context_images=None if images == "all" else int(images) if images else 1,
                web_search=env.get("WITX_WEB_SEARCH", "on").lower() not in ("off", "0", "false"),
                compress_after_steps=_int("WITX_COMPRESS_AFTER_STEPS", 40),
                compress_at_tokens=_int("WITX_COMPRESS_AT_TOKENS", 25_000),
                compress_keep_steps=_int("WITX_COMPRESS_KEEP_STEPS", 8),
            ),
        )
        if settings.default_model not in settings.catalog():
            raise ValueError(f"WITX_DEFAULT_MODEL={settings.default_model} is not in WITX_MODELS")
        return settings
