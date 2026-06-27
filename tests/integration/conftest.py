"""Shared fixtures for P-Ork Gateway integration tests."""
import json
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient


def _write_agent(agents_dir: Path, name: str, model: str = "anthropic/claude-sonnet-4-6"):
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.dump({"name": name, "model": model, "max_tokens": 1024})
    )
    (agent_dir / "soul.md").write_text(f"You are the {name} agent.")
    return agent_dir


@pytest.fixture(scope="session")
def gateway_session(tmp_path_factory):
    """Session-scoped TestClient fixture.

    Uses a real FastAPI lifespan (no mcp_servers → no subprocess spawning).
    The operator token is read from the identity files written by bootstrap_identity.
    """
    tmp = tmp_path_factory.mktemp("gateway")
    identity_dir = tmp / "identity"
    agents_dir = tmp / "agents"
    agents_dir.mkdir()

    _write_agent(agents_dir, "sre-triage")
    _write_agent(agents_dir, "post-mortem")

    config_data = {
        "server": {"host": "0.0.0.0", "port": 18780},
        "identity": {"path": str(identity_dir)},
        "providers": {"anthropic": {"api_key": "test-key"}},
        "agents_dir": str(agents_dir),
        # no mcp_servers → MCPManager.start_all() is a no-op
    }
    config_file = tmp / "config.yaml"
    config_file.write_text(yaml.dump(config_data))

    import os
    os.environ["PORK_GATEWAY_CONFIG"] = str(config_file)

    from gateway.main import app
    with TestClient(app) as client:
        # Read the operator token written by bootstrap_identity
        auth_file = identity_dir / "device-auth.json"
        auth_data = json.loads(auth_file.read_text())
        operator_token = auth_data["tokens"]["operator"]["token"]
        yield client, operator_token, agents_dir
