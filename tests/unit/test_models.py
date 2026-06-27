"""Tests for AgentConfig, GatewayConfig, and related Pydantic models."""
import pytest
from pydantic import ValidationError

from gateway.models.agent import AgentConfig
from gateway.models.config import (
    GatewayConfig,
    IdentityConfig,
    LimitsConfig,
    ProviderConfig,
    ProvidersConfig,
    ServerConfig,
)


class TestAgentConfig:
    def test_minimal_creation(self):
        agent = AgentConfig(name="my-agent", model="anthropic/claude-sonnet-4-6", max_tokens=4096)
        assert agent.name == "my-agent"
        assert agent.model == "anthropic/claude-sonnet-4-6"
        assert agent.max_tokens == 4096
        assert agent.model_fallbacks == []
        assert agent.tools == []
        assert agent.soul == ""

    def test_with_fallbacks_and_soul(self):
        agent = AgentConfig(
            name="sre-triage",
            model="anthropic/claude-sonnet-4-6",
            max_tokens=8192,
            model_fallbacks=["anthropic/claude-haiku-4-5", "openrouter/deepseek/deepseek-chat"],
            soul="You are a helpful SRE.",
        )
        assert agent.model_fallbacks == [
            "anthropic/claude-haiku-4-5",
            "openrouter/deepseek/deepseek-chat",
        ]
        assert agent.soul == "You are a helpful SRE."

    def test_requires_name(self):
        with pytest.raises(ValidationError):
            AgentConfig(model="anthropic/claude-sonnet-4-6", max_tokens=1024)

    def test_requires_model(self):
        with pytest.raises(ValidationError):
            AgentConfig(name="agent", max_tokens=1024)

    def test_max_tokens_has_default(self):
        agent = AgentConfig(name="agent", model="anthropic/claude-sonnet-4-6")
        assert agent.max_tokens == 8192

    def test_tool_scopes_bare_names(self):
        agent = AgentConfig(
            name="a", model="anthropic/claude-sonnet-4-6", max_tokens=1024,
            tools=["grafana", "atlassian"],
        )
        scopes = agent.tool_scopes()
        assert scopes == {"grafana": None, "atlassian": None}

    def test_tool_scopes_scoped_entry(self):
        agent = AgentConfig(
            name="a", model="anthropic/claude-sonnet-4-6", max_tokens=1024,
            tools=[{"atlassian": ["jira_search", "jira_get_issue"]}],
        )
        scopes = agent.tool_scopes()
        assert scopes == {"atlassian": ["jira_search", "jira_get_issue"]}

    def test_tool_scopes_mixed(self):
        agent = AgentConfig(
            name="a", model="anthropic/claude-sonnet-4-6", max_tokens=1024,
            tools=["filesystem", {"atlassian": ["jira_search"]}],
        )
        scopes = agent.tool_scopes()
        assert scopes["filesystem"] is None
        assert scopes["atlassian"] == ["jira_search"]

    def test_tool_scopes_empty(self):
        agent = AgentConfig(name="a", model="anthropic/claude-sonnet-4-6", max_tokens=1024)
        assert agent.tool_scopes() == {}


class TestLimitsConfig:
    def test_defaults(self):
        limits = LimitsConfig()
        assert limits.max_agent_iterations == 20
        assert limits.request_timeout_seconds == 180
        assert limits.mcp_tool_timeout_seconds == 30
        assert limits.llm_retry_attempts == 2
        assert limits.llm_retry_base_delay_seconds == 1.0
        assert limits.max_concurrent_runs == 10

    def test_overrides(self):
        limits = LimitsConfig(max_agent_iterations=5, llm_retry_attempts=0, max_concurrent_runs=3)
        assert limits.max_agent_iterations == 5
        assert limits.llm_retry_attempts == 0
        assert limits.max_concurrent_runs == 3


class TestProvidersConfig:
    def test_all_default_empty(self):
        cfg = ProvidersConfig()
        assert cfg.anthropic.api_key == ""
        assert cfg.openrouter.api_key == ""
        assert cfg.ollama.api_key == ""
        assert cfg.ollama_cloud.api_key == ""
        assert cfg.google.api_key == ""

    def test_alias_ollama_local(self):
        # "ollama-local" YAML key maps to the `ollama` field via alias
        cfg = ProvidersConfig(**{"ollama-local": {"api_key": "local-key"}})
        assert cfg.ollama.api_key == "local-key"

    def test_alias_ollama_cloud(self):
        cfg = ProvidersConfig(**{"ollama-cloud": {"api_key": "cloud-key"}})
        assert cfg.ollama_cloud.api_key == "cloud-key"

    def test_provider_with_base_url(self):
        cfg = ProvidersConfig(openrouter=ProviderConfig(api_key="key", base_url="https://custom.url"))
        assert cfg.openrouter.base_url == "https://custom.url"


class TestGatewayConfig:
    def test_minimal_valid(self):
        cfg = GatewayConfig(
            server=ServerConfig(),
            identity=IdentityConfig(),
            providers=ProvidersConfig(),
        )
        assert cfg.agents_dir == "./agents"
        assert cfg.limits.max_concurrent_runs == 10

    def test_requires_server(self):
        with pytest.raises(ValidationError):
            GatewayConfig(identity=IdentityConfig(), providers=ProvidersConfig())

    def test_requires_identity(self):
        with pytest.raises(ValidationError):
            GatewayConfig(server=ServerConfig(), providers=ProvidersConfig())

    def test_requires_providers(self):
        with pytest.raises(ValidationError):
            GatewayConfig(server=ServerConfig(), identity=IdentityConfig())

    def test_mcp_servers_default_empty(self):
        cfg = GatewayConfig(
            server=ServerConfig(), identity=IdentityConfig(), providers=ProvidersConfig()
        )
        assert cfg.mcp_servers == {}
