"""Tests for gateway/agent_writer.py — the atomic validated-write path for
agent.yaml/soul.md pairs (mirrors P-Ork's config_writer.py test coverage)."""
import yaml

from gateway.agent_writer import delete_agent, validate_agent, write_agent
from gateway.models.config import (
    AzureConfig,
    GatewayConfig,
    IdentityConfig,
    MCPServerConfig,
    ProviderConfig,
    ProvidersConfig,
    ServerConfig,
)


def _make_config(anthropic_key="test-key", mcp_servers=None):
    return GatewayConfig(
        server=ServerConfig(),
        identity=IdentityConfig(),
        providers=ProvidersConfig(anthropic=ProviderConfig(api_key=anthropic_key)),
        mcp_servers=mcp_servers or {},
    )


def _write_on_disk(agents_dir, name, model="anthropic/claude-sonnet-4-6", tools=None):
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.dump({"name": name, "model": model, "max_tokens": 1024, "tools": tools or []})
    )
    (agent_dir / "soul.md").write_text(f"You are {name}.")
    return agent_dir


class TestWriteAgentCreate:
    def test_create_succeeds(self, tmp_path):
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "new-agent",
            yaml.dump({"name": "new-agent", "model": "anthropic/claude-sonnet-4-6"}),
            "You are new-agent.",
            is_update=False,
        )
        assert result.ok
        assert (tmp_path / "new-agent" / "agent.yaml").exists()
        assert (tmp_path / "new-agent" / "soul.md").read_text() == "You are new-agent."

    def test_create_collision_without_overwrite(self, tmp_path):
        _write_on_disk(tmp_path, "existing")
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "existing",
            yaml.dump({"name": "existing", "model": "anthropic/claude-sonnet-4-6"}),
            "New soul.",
            is_update=False,
        )
        assert not result.ok
        assert result.error_type == "collision"

    def test_create_overwrite_true_succeeds(self, tmp_path):
        _write_on_disk(tmp_path, "existing")
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "existing",
            yaml.dump({"name": "existing", "model": "anthropic/claude-sonnet-4-6"}),
            "New soul.",
            is_update=False, overwrite=True,
        )
        assert result.ok
        assert (tmp_path / "existing" / "soul.md").read_text() == "New soul."

    def test_name_field_mismatch_rejected(self, tmp_path):
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "outer-name",
            yaml.dump({"name": "inner-name", "model": "anthropic/claude-sonnet-4-6"}),
            "soul",
            is_update=False,
        )
        assert not result.ok
        assert result.error_type == "validation"

    def test_invalid_yaml_rejected(self, tmp_path):
        config = _make_config()
        result = write_agent(str(tmp_path), config, "bad", "name: [unclosed", "soul", is_update=False)
        assert not result.ok
        assert result.error_type == "validation"

    def test_missing_model_field_rejected(self, tmp_path):
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "no-model",
            yaml.dump({"name": "no-model"}),
            "soul", is_update=False,
        )
        assert not result.ok
        assert result.error_type == "validation"
        assert "errors" in result.error_detail

    def test_unconfigured_mcp_tool_server_rejected(self, tmp_path):
        config = _make_config(mcp_servers={})
        result = write_agent(
            str(tmp_path), config, "tool-user",
            yaml.dump({"name": "tool-user", "model": "anthropic/claude-sonnet-4-6", "tools": ["nonexistent-server"]}),
            "soul", is_update=False,
        )
        assert not result.ok
        assert result.error_type == "validation"
        assert any("nonexistent-server" in e["message"] for e in result.error_detail["errors"])

    def test_configured_mcp_tool_server_accepted(self, tmp_path):
        config = _make_config(mcp_servers={"filesystem": MCPServerConfig(command="npx")})
        result = write_agent(
            str(tmp_path), config, "tool-user",
            yaml.dump({"name": "tool-user", "model": "anthropic/claude-sonnet-4-6", "tools": ["filesystem"]}),
            "soul", is_update=False,
        )
        assert result.ok

    def test_unrecognized_model_prefix_rejected(self, tmp_path):
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "bad-model",
            yaml.dump({"name": "bad-model", "model": "made-up-provider/some-model"}),
            "soul", is_update=False,
        )
        assert not result.ok
        assert result.error_type == "validation"

    def test_recognized_provider_missing_key_is_not_blocking(self, tmp_path):
        # A provider that's a known prefix but has no api_key configured yet is
        # a warning at startup, not a hard write-time rejection (§ validate_agent_models
        # severity split) — the key may simply not be set yet.
        config = _make_config(anthropic_key="")
        result = write_agent(
            str(tmp_path), config, "future-agent",
            yaml.dump({"name": "future-agent", "model": "openrouter/deepseek/deepseek-chat"}),
            "soul", is_update=False,
        )
        assert result.ok

    def test_write_does_not_fail_on_preexisting_unrelated_agent_issue(self, tmp_path):
        # An already-broken agent sitting on disk (unconfigured tool server)
        # must not block writing an unrelated, valid new agent.
        _write_agent_dir_raw(tmp_path, "already-broken",
                              {"name": "already-broken", "model": "anthropic/claude-sonnet-4-6",
                               "tools": ["missing-server"]})
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "fine-agent",
            yaml.dump({"name": "fine-agent", "model": "anthropic/claude-sonnet-4-6"}),
            "soul", is_update=False,
        )
        assert result.ok


def _write_agent_dir_raw(agents_dir, name, agent_yaml_dict):
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.yaml").write_text(yaml.dump(agent_yaml_dict))
    (agent_dir / "soul.md").write_text(f"You are {name}.")


class TestWriteAgentUpdate:
    def test_update_missing_agent_404(self, tmp_path):
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "ghost",
            yaml.dump({"name": "ghost", "model": "anthropic/claude-sonnet-4-6"}),
            "soul", is_update=True,
        )
        assert not result.ok
        assert result.error_type == "not_found"

    def test_update_succeeds(self, tmp_path):
        _write_on_disk(tmp_path, "updateme")
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "updateme",
            yaml.dump({"name": "updateme", "model": "anthropic/claude-sonnet-4-6", "max_tokens": 999}),
            "Updated soul.",
            is_update=True,
        )
        assert result.ok
        assert (tmp_path / "updateme" / "soul.md").read_text() == "Updated soul."
        assert "max_tokens: 999" in (tmp_path / "updateme" / "agent.yaml").read_text()

    def test_update_rejects_renamed_yaml(self, tmp_path):
        _write_on_disk(tmp_path, "stable-name")
        config = _make_config()
        result = write_agent(
            str(tmp_path), config, "stable-name",
            yaml.dump({"name": "renamed", "model": "anthropic/claude-sonnet-4-6"}),
            "soul", is_update=True,
        )
        assert not result.ok
        assert result.error_type == "validation"

    def test_failed_update_leaves_original_files_untouched(self, tmp_path):
        # Rollback guarantee (SPEC-gateway-mcp.md §3): a candidate that fails
        # validation must never partially overwrite what's on disk.
        agent_dir = _write_on_disk(tmp_path, "protected", model="anthropic/claude-sonnet-4-6")
        original_yaml = (agent_dir / "agent.yaml").read_text()
        original_soul = (agent_dir / "soul.md").read_text()

        config = _make_config(mcp_servers={})
        result = write_agent(
            str(tmp_path), config, "protected",
            yaml.dump({"name": "protected", "model": "anthropic/claude-sonnet-4-6",
                       "tools": ["unconfigured-server"]}),
            "This soul must never be written.",
            is_update=True,
        )
        assert not result.ok
        assert (agent_dir / "agent.yaml").read_text() == original_yaml
        assert (agent_dir / "soul.md").read_text() == original_soul


class TestDeleteAgent:
    def test_delete_missing_404(self, tmp_path):
        result = delete_agent(str(tmp_path), "ghost")
        assert not result.ok
        assert result.error_type == "not_found"

    def test_delete_returns_prior_content(self, tmp_path):
        _write_on_disk(tmp_path, "doomed")
        result = delete_agent(str(tmp_path), "doomed")
        assert result.ok
        assert "doomed" in result.config["agent_yaml"]
        assert result.config["soul_md"] == "You are doomed."
        assert not (tmp_path / "doomed").exists()


class TestValidateAgent:
    def test_valid_agent_no_write(self, tmp_path):
        config = _make_config()
        valid, errors = validate_agent(
            str(tmp_path), config,
            yaml.dump({"name": "dry-run", "model": "anthropic/claude-sonnet-4-6"}),
            "soul",
        )
        assert valid
        assert errors == []
        assert not (tmp_path / "dry-run").exists()

    def test_invalid_agent_reports_errors(self, tmp_path):
        config = _make_config()
        valid, errors = validate_agent(
            str(tmp_path), config,
            yaml.dump({"name": "dry-run"}),  # missing required 'model'
            "soul",
        )
        assert not valid
        assert errors

    def test_unconfigured_tool_server_reported(self, tmp_path):
        config = _make_config()
        valid, errors = validate_agent(
            str(tmp_path), config,
            yaml.dump({"name": "dry-run", "model": "anthropic/claude-sonnet-4-6", "tools": ["ghost-server"]}),
            "soul",
        )
        assert not valid
        assert any("ghost-server" in e["msg"] for e in errors)
