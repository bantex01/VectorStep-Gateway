"""Tests for LLMRouter — model string prefix routing and provider caching."""
import pytest

from gateway.llm.providers.anthropic import AnthropicProvider
from gateway.llm.providers.azure import AzureOpenAIProvider
from gateway.llm.providers.google import GoogleProvider
from gateway.llm.providers.ollama import OllamaCloudProvider, OllamaProvider
from gateway.llm.providers.openai import OpenAIProvider
from gateway.llm.providers.openrouter import OpenRouterProvider
from gateway.llm.router import LLMRouter, provider_key_for_model_string
from gateway.models.config import (
    AzureConfig,
    GatewayConfig,
    IdentityConfig,
    LimitsConfig,
    ProviderConfig,
    ProvidersConfig,
    ServerConfig,
)


def _config(**provider_keys):
    """Build a GatewayConfig with specified provider api_keys."""
    providers_kwargs = {k: ProviderConfig(api_key=v) for k, v in provider_keys.items()}
    return GatewayConfig(
        server=ServerConfig(),
        identity=IdentityConfig(),
        providers=ProvidersConfig(**providers_kwargs),
        limits=LimitsConfig(),
    )


def _azure_config(api_key="key", resource_name="my-resource", api_version="2025-01-01-preview"):
    return GatewayConfig(
        server=ServerConfig(),
        identity=IdentityConfig(),
        providers=ProvidersConfig(
            azure=AzureConfig(api_key=api_key, resource_name=resource_name, api_version=api_version)
        ),
        limits=LimitsConfig(),
    )


class TestRouting:
    def test_anthropic_prefix(self):
        router = LLMRouter(_config(anthropic="key"))
        provider, model = router.get_provider_and_model("anthropic/claude-sonnet-4-6")
        assert isinstance(provider, AnthropicProvider)
        assert model == "claude-sonnet-4-6"

    def test_openrouter_prefix(self):
        router = LLMRouter(_config(openrouter="key"))
        provider, model = router.get_provider_and_model("openrouter/deepseek/deepseek-chat")
        assert isinstance(provider, OpenRouterProvider)
        assert model == "deepseek/deepseek-chat"

    def test_ollama_local_prefix(self):
        router = LLMRouter(_config())
        provider, model = router.get_provider_and_model("ollama/qwen3:8b")
        assert isinstance(provider, OllamaProvider)
        assert model == "qwen3:8b"

    def test_ollama_cloud_prefix(self):
        cfg = GatewayConfig(
            server=ServerConfig(),
            identity=IdentityConfig(),
            providers=ProvidersConfig(**{"ollama-cloud": ProviderConfig(api_key="cloud-key")}),
        )
        router = LLMRouter(cfg)
        provider, model = router.get_provider_and_model("ollama-cloud/gemma3:27b")
        assert isinstance(provider, OllamaCloudProvider)
        assert model == "gemma3:27b"

    def test_google_prefix(self):
        router = LLMRouter(_config(google="key"))
        provider, model = router.get_provider_and_model("google/gemini-2.0-flash")
        assert isinstance(provider, GoogleProvider)
        assert model == "gemini-2.0-flash"

    def test_azure_prefix(self):
        router = LLMRouter(_azure_config())
        provider, model = router.get_provider_and_model("azure/gpt-4o")
        assert isinstance(provider, AzureOpenAIProvider)
        assert model == "gpt-4o"

    def test_azure_prefix_mini(self):
        router = LLMRouter(_azure_config())
        provider, model = router.get_provider_and_model("azure/gpt-4o-mini")
        assert isinstance(provider, AzureOpenAIProvider)
        assert model == "gpt-4o-mini"

    def test_openai_prefix(self):
        router = LLMRouter(_config(openai="key"))
        provider, model = router.get_provider_and_model("openai/gpt-5")
        assert isinstance(provider, OpenAIProvider)
        assert model == "gpt-5"

    def test_bare_name_defaults_to_anthropic(self):
        router = LLMRouter(_config(anthropic="key"))
        provider, model = router.get_provider_and_model("claude-sonnet-4-6")
        assert isinstance(provider, AnthropicProvider)
        assert model == "claude-sonnet-4-6"

    def test_anthropic_prefix_with_nested_slash(self):
        router = LLMRouter(_config(anthropic="key"))
        provider, model = router.get_provider_and_model("anthropic/us.anthropic.claude-sonnet-4-6-v1:0")
        assert isinstance(provider, AnthropicProvider)
        assert model == "us.anthropic.claude-sonnet-4-6-v1:0"


class TestProviderCaching:
    def test_azure_same_instance_returned(self):
        router = LLMRouter(_azure_config())
        p1, _ = router.get_provider_and_model("azure/gpt-4o")
        p2, _ = router.get_provider_and_model("azure/gpt-4o-mini")
        assert p1 is p2

    def test_same_instance_returned_for_same_type(self):
        router = LLMRouter(_config(anthropic="key"))
        p1, _ = router.get_provider_and_model("anthropic/claude-sonnet-4-6")
        p2, _ = router.get_provider_and_model("anthropic/claude-haiku-4-5")
        assert p1 is p2

    def test_different_instances_for_different_types(self):
        router = LLMRouter(_config(anthropic="key", openrouter="key2"))
        p_anthropic, _ = router.get_provider_and_model("anthropic/claude-sonnet-4-6")
        p_openrouter, _ = router.get_provider_and_model("openrouter/some-model")
        assert p_anthropic is not p_openrouter
        assert isinstance(p_anthropic, AnthropicProvider)
        assert isinstance(p_openrouter, OpenRouterProvider)

    def test_fallback_chain_crosses_into_and_out_of_openai(self):
        # Simulates model_fallbacks resolution: anthropic/... -> openai/... -> anthropic/...
        router = LLMRouter(_config(anthropic="key", openai="key2"))
        chain = ["anthropic/claude-sonnet-4-6", "openai/gpt-5", "anthropic/claude-haiku-4-5"]
        providers = [router.get_provider_and_model(m)[0] for m in chain]
        assert isinstance(providers[0], AnthropicProvider)
        assert isinstance(providers[1], OpenAIProvider)
        assert isinstance(providers[2], AnthropicProvider)
        # Same-type steps still share the cached instance.
        assert providers[0] is providers[2]
        assert providers[0] is not providers[1]


class TestProviderKeyForModelString:
    def test_openai_prefix(self):
        assert provider_key_for_model_string("openai/gpt-5") == "openai"

    def test_anthropic_prefix(self):
        assert provider_key_for_model_string("anthropic/claude-sonnet-4-6") == "anthropic"

    def test_bare_name_defaults_to_anthropic(self):
        assert provider_key_for_model_string("claude-sonnet-4-6") == "anthropic"
