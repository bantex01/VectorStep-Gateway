"""Tests for config loading and env-var resolution."""
import os

import pytest
import yaml

from gateway.models.config import _resolve_env_vars, load_config


class TestResolveEnvVars:
    def test_plain_string_no_substitution(self):
        assert _resolve_env_vars("hello") == "hello"

    def test_string_substitution(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret")
        assert _resolve_env_vars("${MY_KEY}") == "secret"

    def test_unset_var_becomes_empty_string(self, monkeypatch):
        monkeypatch.delenv("UNSET_VAR_XYZ", raising=False)
        assert _resolve_env_vars("${UNSET_VAR_XYZ}") == ""

    def test_partial_substitution(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc")
        assert _resolve_env_vars("Bearer ${TOKEN}") == "Bearer abc"

    def test_nested_dict(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "my-key")
        result = _resolve_env_vars({"providers": {"anthropic": {"api_key": "${API_KEY}"}}})
        assert result == {"providers": {"anthropic": {"api_key": "my-key"}}}

    def test_list(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        result = _resolve_env_vars(["${HOST}", "other"])
        assert result == ["localhost", "other"]

    def test_non_string_passthrough(self):
        assert _resolve_env_vars(42) == 42
        assert _resolve_env_vars(3.14) == 3.14
        assert _resolve_env_vars(True) is True
        assert _resolve_env_vars(None) is None

    def test_multiple_vars_in_string(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "5432")
        result = _resolve_env_vars("postgresql://${HOST}:${PORT}/db")
        assert result == "postgresql://localhost:5432/db"


class TestLoadConfig:
    def test_loads_minimal_config(self, tmp_path):
        config_data = {
            "server": {"host": "0.0.0.0", "port": 18780},
            "identity": {"path": str(tmp_path / "identity")},
            "providers": {"anthropic": {"api_key": "test-key"}},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 18780
        assert cfg.providers.anthropic.api_key == "test-key"

    def test_resolves_env_vars_on_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_ANTHROPIC_KEY", "resolved-key")
        config_data = {
            "server": {"host": "0.0.0.0", "port": 18780},
            "identity": {"path": str(tmp_path / "identity")},
            "providers": {"anthropic": {"api_key": "${TEST_ANTHROPIC_KEY}"}},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))
        assert cfg.providers.anthropic.api_key == "resolved-key"

    def test_agents_dir_default(self, tmp_path):
        config_data = {
            "server": {"host": "0.0.0.0", "port": 18780},
            "identity": {"path": str(tmp_path / "identity")},
            "providers": {},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))
        assert cfg.agents_dir == "./agents"

    def test_limits_override(self, tmp_path):
        config_data = {
            "server": {"host": "0.0.0.0", "port": 18780},
            "identity": {"path": str(tmp_path / "identity")},
            "providers": {},
            "limits": {"max_concurrent_runs": 5, "llm_retry_attempts": 0},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))
        assert cfg.limits.max_concurrent_runs == 5
        assert cfg.limits.llm_retry_attempts == 0
