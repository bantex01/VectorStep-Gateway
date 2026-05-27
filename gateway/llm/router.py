from __future__ import annotations

import logging

from gateway.llm.providers.anthropic import AnthropicProvider
from gateway.llm.providers.base import BaseProvider
from gateway.llm.providers.google import GoogleProvider
from gateway.llm.providers.ollama import OllamaProvider
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
        if model_string.startswith("ollama/"):
            return self._ollama(), model_string[len("ollama/"):]
        if model_string.startswith("ollama-cloud/"):
            return self._ollama_cloud(), model_string[len("ollama-cloud/"):]
        if model_string.startswith("google/"):
            return self._google(), model_string[len("google/"):]
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

    def _ollama(self) -> OllamaProvider:
        if "ollama" not in self._providers:
            cfg = self._config.providers.ollama
            self._providers["ollama"] = OllamaProvider(
                base_url=cfg.base_url or "http://localhost:11434/v1",
                api_key=cfg.api_key,
                timeout=float(self._config.limits.request_timeout_seconds),
            )
        return self._providers["ollama"]  # type: ignore[return-value]

    def _ollama_cloud(self) -> OllamaProvider:
        if "ollama_cloud" not in self._providers:
            cfg = self._config.providers.ollama_cloud
            self._providers["ollama_cloud"] = OllamaProvider(
                base_url=cfg.base_url or "https://ollama.com/api",
                api_key=cfg.api_key,
                timeout=float(self._config.limits.request_timeout_seconds),
            )
        return self._providers["ollama_cloud"]  # type: ignore[return-value]

    def _google(self) -> GoogleProvider:
        if "google" not in self._providers:
            cfg = self._config.providers.google
            self._providers["google"] = GoogleProvider(
                base_url=cfg.base_url or "https://generativelanguage.googleapis.com/v1beta/openai",
                api_key=cfg.api_key,
                timeout=float(self._config.limits.request_timeout_seconds),
            )
        return self._providers["google"]  # type: ignore[return-value]
