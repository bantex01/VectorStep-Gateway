from __future__ import annotations

import logging

from gateway.llm.providers.anthropic import AnthropicProvider
from gateway.llm.providers.azure import AzureOpenAIProvider
from gateway.llm.providers.base import BaseProvider
from gateway.llm.providers.google import GoogleProvider
from gateway.llm.providers.ollama import OllamaCloudProvider, OllamaProvider
from gateway.llm.providers.openai import OpenAIProvider
from gateway.llm.providers.openrouter import OpenRouterProvider
from gateway.llm.providers.yolo import YoloProvider
from gateway.models.config import GatewayConfig

logger = logging.getLogger(__name__)

# Canonical prefix → provider-key mapping. Single source of truth for "which
# provider actually served this model string" — used by agent_runner.py to
# report the provider that served a request (including after a cross-provider
# model_fallback) and by agents/loader.py to validate agent.yaml model strings.
# Must stay in sync with get_provider_and_model below.
PREFIX_TO_PROVIDER: dict[str, str] = {
    "anthropic/": "anthropic",
    "openrouter/": "openrouter",
    "ollama/": "ollama",              # ollama-local in config
    "ollama-cloud/": "ollama_cloud",
    "google/": "google",
    "azure/": "azure",
    "openai/": "openai",
    "yolo/": "yolo",
}


# Provider keys whose complete() actually acts on thinking_level. Every other
# provider logs a warning and ignores it (see each provider's complete()), so an
# agent pinned to one of them never thinks no matter what agent.yaml says —
# which is what validate_agent_models surfaces once at load time instead of on
# every LLM call. Add a key here the moment that provider maps thinking_level to
# its own reasoning parameter.
THINKING_CAPABLE_PROVIDERS: frozenset[str] = frozenset({"anthropic"})


def provider_key_for_model_string(model_string: str) -> str:
    """Return the provider key that get_provider_and_model would route to.

    Mirrors get_provider_and_model's own prefix matching (including the
    bare-name-defaults-to-anthropic rule) without needing a live LLMRouter
    instance — used to report which provider actually served a request.
    """
    for prefix, provider_key in PREFIX_TO_PROVIDER.items():
        if model_string.startswith(prefix):
            return provider_key
    return "anthropic"


class LLMRouter:
    """Routes a model string to the appropriate provider, caching one instance per type."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._providers: dict[str, BaseProvider] = {}

    async def aclose(self) -> None:
        """Close every provider instantiated so far (releases pooled HTTP connections)."""
        for provider in self._providers.values():
            await provider.aclose()

    def get_provider_and_model(self, model_string: str) -> tuple[BaseProvider, str]:
        """Return (provider, bare_model_name) for the given model string.

        Prefix rules:
          openrouter/<model>  → OpenRouterProvider,    model = everything after prefix
          anthropic/<model>   → AnthropicProvider,     model = everything after prefix
          azure/<model>       → AzureOpenAIProvider,   model = deployment name after prefix
          google/<model>      → GoogleProvider,        model = everything after prefix
          ollama/<model>      → OllamaProvider,        model = everything after prefix
          ollama-cloud/<model>→ OllamaCloudProvider,   model = everything after prefix
          openai/<model>      → OpenAIProvider,        model = everything after prefix
          yolo/<model>        → YoloProvider,          model = everything after prefix
          <bare>              → AnthropicProvider,     model = <bare>
        """
        if model_string.startswith("openrouter/"):
            return self._openrouter(), model_string[len("openrouter/"):]
        if model_string.startswith("anthropic/"):
            return self._anthropic(), model_string[len("anthropic/"):]
        if model_string.startswith("azure/"):
            return self._azure(), model_string[len("azure/"):]
        if model_string.startswith("ollama/"):
            return self._ollama(), model_string[len("ollama/"):]
        if model_string.startswith("ollama-cloud/"):
            return self._ollama_cloud(), model_string[len("ollama-cloud/"):]
        if model_string.startswith("google/"):
            return self._google(), model_string[len("google/"):]
        if model_string.startswith("openai/"):
            return self._openai(), model_string[len("openai/"):]
        if model_string.startswith("yolo/"):
            return self._yolo(), model_string[len("yolo/"):]
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

    def _ollama_cloud(self) -> OllamaCloudProvider:
        if "ollama_cloud" not in self._providers:
            cfg = self._config.providers.ollama_cloud
            self._providers["ollama_cloud"] = OllamaCloudProvider(
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

    def _azure(self) -> AzureOpenAIProvider:
        if "azure" not in self._providers:
            cfg = self._config.providers.azure
            self._providers["azure"] = AzureOpenAIProvider(
                api_key=cfg.api_key,
                resource_name=cfg.resource_name,
                api_version=cfg.api_version,
                timeout=float(self._config.limits.request_timeout_seconds),
            )
        return self._providers["azure"]  # type: ignore[return-value]

    def _openai(self) -> OpenAIProvider:
        if "openai" not in self._providers:
            cfg = self._config.providers.openai
            self._providers["openai"] = OpenAIProvider(
                base_url=cfg.base_url or "https://api.openai.com/v1",
                api_key=cfg.api_key,
                timeout=float(self._config.limits.request_timeout_seconds),
            )
        return self._providers["openai"]  # type: ignore[return-value]

    def _yolo(self) -> YoloProvider:
        if "yolo" not in self._providers:
            cfg = self._config.providers.yolo
            self._providers["yolo"] = YoloProvider(
                base_url=cfg.base_url or "",
                api_key=cfg.api_key,
                timeout=float(self._config.limits.request_timeout_seconds),
            )
        return self._providers["yolo"]  # type: ignore[return-value]
