"""One ModelClient for the whole catalog: each request goes to the provider of the model
it names, under that provider's own model ID. A task that switches models therefore
reaches the other provider on its next step, with the same history. Nothing reaches a
provider the tenant does not allow."""

from dataclasses import replace

from witx.config import CatalogModel
from witx.domain import rules
from witx.domain.rules import Rejected
from witx.models.base import ModelClient, ModelError, ModelRequest, ModelResponse


class ModelRouter:
    def __init__(
        self,
        catalog: dict[str, CatalogModel],
        providers: dict[str, ModelClient],
        default_model: str,
    ) -> None:
        self.catalog = catalog
        self.providers = providers
        self.default_provider = catalog[default_model].provider

    async def ready(self, model: str) -> str:
        entry = self.catalog.get(model)
        if entry is None:
            return "unknown_model"
        client = self.providers.get(entry.provider)
        if client is None:
            return "provider_not_configured"
        return await client.ready(entry.provider_model)

    async def respond(self, request: ModelRequest) -> ModelResponse:
        entry = self.catalog.get(request.model)
        if entry is None:
            raise ModelError("unknown_model", request.model, retryable=False)
        allowed = rules.allowed_providers(request.allowed_providers, self.default_provider)
        try:
            rules.check_provider(entry.provider, allowed)
        except Rejected as error:
            raise ModelError(error.code, error.detail, retryable=False) from None
        client = self.providers.get(entry.provider)
        if client is None:
            raise ModelError("provider_not_configured", entry.provider, retryable=False)
        return await client.respond(replace(request, model=entry.provider_model))
