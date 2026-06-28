"""Tests for agent loading and startup model validation."""
import logging

import pytest
import yaml

from gateway.agents.loader import load_agents, validate_agent_models
from gateway.models.agent import AgentConfig
from gateway.models.config import (
    AzureConfig,
    GatewayConfig,
    IdentityConfig,
    ProvidersConfig,
    ProviderConfig,
    ServerConfig,
)


def _make_config(anthropic_key="", openrouter_key="", ollama_cloud_key="", google_key="",
                 azure_key="", azure_resource=""):
    return GatewayConfig(
        server=ServerConfig(),
        identity=IdentityConfig(),
        providers=ProvidersConfig(
            anthropic=ProviderConfig(api_key=anthropic_key),
            openrouter=ProviderConfig(api_key=openrouter_key),
            **{"ollama-cloud": ProviderConfig(api_key=ollama_cloud_key)},
            google=ProviderConfig(api_key=google_key),
            azure=AzureConfig(api_key=azure_key, resource_name=azure_resource),
        ),
    )


def _make_agent(model, fallbacks=None):
    return AgentConfig(
        name="test-agent",
        model=model,
        max_tokens=1024,
        model_fallbacks=fallbacks or [],
    )


class TestValidateAgentModels:
    def test_configured_anthropic_no_warning(self, caplog):
        config = _make_config(anthropic_key="sk-ant-test")
        agents = {"triage": _make_agent("anthropic/claude-sonnet-4-6")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert caplog.text == ""

    def test_bare_model_name_treated_as_anthropic(self, caplog):
        config = _make_config(anthropic_key="sk-ant-test")
        agents = {"triage": _make_agent("claude-sonnet-4-6")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert caplog.text == ""

    def test_bare_name_warns_when_anthropic_unconfigured(self, caplog):
        config = _make_config(anthropic_key="")
        agents = {"triage": _make_agent("claude-sonnet-4-6")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert "anthropic" in caplog.text.lower()
        assert "api_key" in caplog.text.lower()

    def test_unconfigured_openrouter_warns(self, caplog):
        config = _make_config(openrouter_key="")
        agents = {"agent": _make_agent("openrouter/deepseek/deepseek-chat")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert "openrouter" in caplog.text.lower()

    def test_configured_openrouter_no_warning(self, caplog):
        config = _make_config(openrouter_key="or-key")
        agents = {"agent": _make_agent("openrouter/deepseek/deepseek-chat")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert caplog.text == ""

    def test_ollama_local_exempt_from_key_check(self, caplog):
        # Local Ollama never needs an api_key — should never warn regardless of config
        config = _make_config()
        agents = {"agent": _make_agent("ollama/qwen3:8b")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert caplog.text == ""

    def test_unconfigured_google_warns(self, caplog):
        config = _make_config(google_key="")
        agents = {"agent": _make_agent("google/gemini-2.0-flash")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert "google" in caplog.text.lower()

    def test_configured_azure_no_warning(self, caplog):
        config = _make_config(azure_key="az-key", azure_resource="my-resource")
        agents = {"agent": _make_agent("azure/gpt-4o")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert caplog.text == ""

    def test_unconfigured_azure_warns(self, caplog):
        config = _make_config(azure_key="", azure_resource="")
        agents = {"agent": _make_agent("azure/gpt-4o")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert "azure" in caplog.text.lower()

    def test_azure_missing_resource_warns(self, caplog):
        config = _make_config(azure_key="az-key", azure_resource="")
        agents = {"agent": _make_agent("azure/gpt-4o")}
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert "azure" in caplog.text.lower()

    def test_unrecognized_prefix_logs_error(self, caplog):
        config = _make_config(anthropic_key="key")
        agents = {"agent": _make_agent("my-custom-provider/some-model")}
        with caplog.at_level(logging.ERROR, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert "unrecognized" in caplog.text.lower()
        assert "my-custom-provider/some-model" in caplog.text

    def test_fallbacks_also_validated(self, caplog):
        # Primary is fine; fallback references an unconfigured provider
        config = _make_config(anthropic_key="key", openrouter_key="")
        agents = {
            "agent": _make_agent(
                "anthropic/claude-sonnet-4-6",
                fallbacks=["openrouter/deepseek/deepseek-chat"],
            )
        }
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models(agents, config)
        assert "openrouter" in caplog.text.lower()

    def test_empty_agents_no_warnings(self, caplog):
        config = _make_config()
        with caplog.at_level(logging.WARNING, logger="gateway.agents.loader"):
            validate_agent_models({}, config)
        assert caplog.text == ""


class TestLoadAgents:
    def _write_agent(self, base: object, name: str, model: str = "anthropic/claude-sonnet-4-6"):
        from pathlib import Path
        agent_dir = Path(str(base)) / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent.yaml").write_text(
            yaml.dump({"name": name, "model": model, "max_tokens": 1024})
        )
        (agent_dir / "soul.md").write_text(f"You are {name}.")
        return agent_dir

    def test_loads_valid_agent(self, tmp_path):
        self._write_agent(tmp_path, "sre-triage")
        agents = load_agents(str(tmp_path))
        assert "sre-triage" in agents
        assert agents["sre-triage"].model == "anthropic/claude-sonnet-4-6"
        assert agents["sre-triage"].soul == "You are sre-triage."

    def test_loads_multiple_agents(self, tmp_path):
        self._write_agent(tmp_path, "triage")
        self._write_agent(tmp_path, "remediation")
        agents = load_agents(str(tmp_path))
        assert set(agents) == {"triage", "remediation"}

    def test_skips_missing_soul(self, tmp_path):
        agent_dir = tmp_path / "no-soul"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            yaml.dump({"name": "no-soul", "model": "anthropic/claude-sonnet-4-6", "max_tokens": 1024})
        )
        agents = load_agents(str(tmp_path))
        assert "no-soul" not in agents

    def test_skips_bad_yaml(self, tmp_path):
        agent_dir = tmp_path / "bad-yaml"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: [unclosed bracket")
        (agent_dir / "soul.md").write_text("soul")
        agents = load_agents(str(tmp_path))
        assert "bad-yaml" not in agents

    def test_skips_name_mismatch(self, tmp_path):
        agent_dir = tmp_path / "my-agent"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            yaml.dump({"name": "wrong-name", "model": "anthropic/claude-sonnet-4-6", "max_tokens": 1024})
        )
        (agent_dir / "soul.md").write_text("soul")
        agents = load_agents(str(tmp_path))
        assert "my-agent" not in agents
        assert "wrong-name" not in agents

    def test_empty_agents_dir(self, tmp_path):
        agents = load_agents(str(tmp_path))
        assert agents == {}

    def test_non_existent_dir_returns_empty(self, tmp_path):
        agents = load_agents(str(tmp_path / "does-not-exist"))
        assert agents == {}
