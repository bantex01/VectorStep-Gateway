from __future__ import annotations

import logging

from gateway.llm.providers.anthropic import AnthropicProvider
from gateway.llm.providers.base import BaseProvider
from gateway.llm.providers.openrouter import OpenRouterProvider
from gateway.models.config import GatewayConfig

logger = logging.getLogger(__name__)


class LLMRouter:
    """Routes a model string to the appropriate provider, caching one instance per type."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._providers: dict[str, BaseProvider] = {}

    def get_provider_and_model(self, model_string: str) -> tuple[BaseProvider, str]:
        """Return (provider, bare_model_name) for the given model string.

        Prefix rules:
          openrouter/<model>  → OpenRouterProvider, model = everything after prefix
          anthropic/<model>   → AnthropicProvider,  model = everything after prefix
          <bare>              → AnthropicProvider,  model = <bare>
        """
        if model_string.startswith("openrouter/"):
            return self._openrouter(), model_string[len("openrouter/"):]
        if model_string.startswith("anthropic/"):
            return self._anthropic(), model_string[len("anthropic/"):]
        return self._anthropic(), model_string

    # ------------------------------------------------------------------
    # Cached provider accessors
    # ------------------------------------------------------------------

    def _anthropic(self) -> AnthropicProvider:
        if "anthropic" not in self._providers:
            self._providers["anthropic"] = AnthropicProvider(
                api_key=self._config.providers.anthropic.api_key,
            )
        return self._providers["anthropic"]  # type: ignore[return-value]

    def _openrouter(self) -> OpenRouterProvider:
        if "openrouter" not in self._providers:
            self._providers["openrouter"] = OpenRouterProvider(
                api_key=self._config.providers.openrouter.api_key,
                timeout=float(self._config.limits.request_timeout_seconds),
            )
        return self._providers["openrouter"]  # type: ignore[return-value]
