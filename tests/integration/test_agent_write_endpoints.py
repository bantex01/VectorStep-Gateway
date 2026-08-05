"""Integration tests for the new agent-management JSON endpoints
(SPEC-gateway-mcp.md §3): GET /agents/{name}, GET /providers,
POST/PUT/DELETE /agents, POST /agents/validate.

These tests create/update/delete agents against a live app, which would
pollute test_http.py's shared session-scoped `gateway_session` fixture (its
assertions hardcode an exact agent count) — so this file gets its own
module-scoped app instance instead of importing that fixture.
"""
import json

import pytest
import yaml
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def gateway_session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gateway-write-tests")
    identity_dir = tmp / "identity"
    agents_dir = tmp / "agents"
    agents_dir.mkdir()

    sre_dir = agents_dir / "sre-triage"
    sre_dir.mkdir()
    (sre_dir / "agent.yaml").write_text(
        yaml.dump({"name": "sre-triage", "model": "anthropic/claude-sonnet-4-6", "max_tokens": 1024})
    )
    (sre_dir / "soul.md").write_text("You are the sre-triage agent.")

    config_data = {
        "server": {"host": "0.0.0.0", "port": 18781},
        "identity": {"path": str(identity_dir)},
        "providers": {"anthropic": {"api_key": "test-key"}},
        "agents_dir": str(agents_dir),
        # no mcp_servers → MCPManager.start_all() is a no-op, no subprocess spawning
        # (matches tests/integration/conftest.py's gateway_session fixture)
    }
    config_file = tmp / "config.yaml"
    config_file.write_text(yaml.dump(config_data))

    import os
    old_config_env = os.environ.get("VECTORSTEP_GATEWAY_CONFIG")
    os.environ["VECTORSTEP_GATEWAY_CONFIG"] = str(config_file)

    # gateway.main is imported fresh relative to this env var by the app's
    # lifespan reading VECTORSTEP_GATEWAY_CONFIG at startup — importing here (after
    # setting the env var) mirrors how tests/integration/conftest.py does it.
    from gateway.main import app
    with TestClient(app) as client:
        auth_file = identity_dir / "device-auth.json"
        auth_data = json.loads(auth_file.read_text())
        operator_token = auth_data["tokens"]["operator"]["token"]
        yield client, operator_token, agents_dir

    if old_config_env is not None:
        os.environ["VECTORSTEP_GATEWAY_CONFIG"] = old_config_env
    else:
        os.environ.pop("VECTORSTEP_GATEWAY_CONFIG", None)


class TestGetAgent:
    def test_returns_combined_view(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/agents/sre-triage")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "sre-triage"
        assert data["config"]["model"] == "anthropic/claude-sonnet-4-6"
        assert "soul" not in data["config"]
        assert "sre-triage" in data["soul_md"]
        assert "model" in data["agent_yaml"]

    def test_404_for_unknown(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/agents/does-not-exist-at-all")
        assert response.status_code == 404


class TestListProviders:
    def test_returns_200(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/providers")
        assert response.status_code == 200

    def test_no_keys_leaked(self, gateway_session):
        client, _, _ = gateway_session
        text = client.get("/providers").content.decode()
        assert "test-key" not in text  # the anthropic api_key configured in the fixture

    def test_shape(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/providers").json()
        names = {p["name"] for p in data["providers"]}
        assert "anthropic" in names
        anthropic = next(p for p in data["providers"] if p["name"] == "anthropic")
        assert anthropic["configured"] is True
        assert "prefix" in anthropic


class TestCreateAgent:
    def test_create_succeeds(self, gateway_session):
        client, _, agents_dir = gateway_session
        response = client.post("/agents", json={
            "name": "wt-create-1",
            "agent_yaml": yaml.dump({"name": "wt-create-1", "model": "anthropic/claude-sonnet-4-6"}),
            "soul_md": "You are wt-create-1.",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["committed"] is False
        assert (agents_dir / "wt-create-1" / "agent.yaml").exists()

        # picked up by the live registry (create implicitly reloads)
        assert client.get("/agents/wt-create-1").status_code == 200

    def test_collision_without_overwrite_409(self, gateway_session):
        client, _, _ = gateway_session
        body = {
            "name": "wt-collide",
            "agent_yaml": yaml.dump({"name": "wt-collide", "model": "anthropic/claude-sonnet-4-6"}),
            "soul_md": "soul",
        }
        assert client.post("/agents", json=body).status_code == 200
        response = client.post("/agents", json=body)
        assert response.status_code == 409
        assert response.json()["detail"]["type"] == "collision"

    def test_overwrite_true_succeeds(self, gateway_session):
        client, _, _ = gateway_session
        body = {
            "name": "wt-overwrite",
            "agent_yaml": yaml.dump({"name": "wt-overwrite", "model": "anthropic/claude-sonnet-4-6"}),
            "soul_md": "v1",
        }
        assert client.post("/agents", json=body).status_code == 200
        body["soul_md"] = "v2"
        body["overwrite"] = True
        response = client.post("/agents", json=body)
        assert response.status_code == 200
        assert client.get("/agents/wt-overwrite").json()["soul_md"] == "v2"

    def test_invalid_yaml_400(self, gateway_session):
        client, _, _ = gateway_session
        response = client.post("/agents", json={
            "name": "wt-invalid", "agent_yaml": "name: [unclosed", "soul_md": "soul",
        })
        assert response.status_code == 400
        assert response.json()["detail"]["type"] == "validation"

    def test_unconfigured_tool_server_400(self, gateway_session):
        client, _, _ = gateway_session
        response = client.post("/agents", json={
            "name": "wt-badtool",
            "agent_yaml": yaml.dump({"name": "wt-badtool", "model": "anthropic/claude-sonnet-4-6",
                                      "tools": ["nonexistent-mcp-server"]}),
            "soul_md": "soul",
        })
        assert response.status_code == 400
        assert response.json()["detail"]["type"] == "validation"

    def test_missing_body_fields_400(self, gateway_session):
        client, _, _ = gateway_session
        response = client.post("/agents", json={"name": "wt-incomplete"})
        assert response.status_code == 400


class TestUpdateAgent:
    def test_update_succeeds(self, gateway_session):
        client, _, _ = gateway_session
        create_body = {
            "name": "wt-update-1",
            "agent_yaml": yaml.dump({"name": "wt-update-1", "model": "anthropic/claude-sonnet-4-6"}),
            "soul_md": "v1",
        }
        assert client.post("/agents", json=create_body).status_code == 200

        response = client.put("/agents/wt-update-1", json={"soul_md": "v2"})
        assert response.status_code == 200
        data = client.get("/agents/wt-update-1").json()
        assert data["soul_md"] == "v2"
        assert data["config"]["model"] == "anthropic/claude-sonnet-4-6"  # agent_yaml untouched

    def test_update_missing_agent_404(self, gateway_session):
        client, _, _ = gateway_session
        response = client.put("/agents/wt-does-not-exist", json={"soul_md": "x"})
        assert response.status_code == 404

    def test_update_rename_rejected(self, gateway_session):
        client, _, _ = gateway_session
        create_body = {
            "name": "wt-rename-src",
            "agent_yaml": yaml.dump({"name": "wt-rename-src", "model": "anthropic/claude-sonnet-4-6"}),
            "soul_md": "soul",
        }
        assert client.post("/agents", json=create_body).status_code == 200
        response = client.put("/agents/wt-rename-src", json={
            "agent_yaml": yaml.dump({"name": "wt-renamed", "model": "anthropic/claude-sonnet-4-6"}),
        })
        assert response.status_code == 400


class TestValidateAgentEndpoint:
    def test_valid_no_write(self, gateway_session):
        client, _, agents_dir = gateway_session
        response = client.post("/agents/validate", json={
            "agent_yaml": yaml.dump({"name": "wt-dry-run", "model": "anthropic/claude-sonnet-4-6"}),
            "soul_md": "soul",
        })
        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert not (agents_dir / "wt-dry-run").exists()

    def test_invalid_reports_errors(self, gateway_session):
        client, _, _ = gateway_session
        response = client.post("/agents/validate", json={
            "agent_yaml": yaml.dump({"name": "wt-dry-run-bad"}),  # missing model
        })
        data = response.json()
        assert data["valid"] is False
        assert data["errors"]


class TestDeleteAgent:
    def test_delete_succeeds_and_returns_prior_content(self, gateway_session):
        client, _, agents_dir = gateway_session
        create_body = {
            "name": "wt-delete-me",
            "agent_yaml": yaml.dump({"name": "wt-delete-me", "model": "anthropic/claude-sonnet-4-6"}),
            "soul_md": "doomed soul",
        }
        assert client.post("/agents", json=create_body).status_code == 200

        response = client.delete("/agents/wt-delete-me")
        assert response.status_code == 200
        data = response.json()
        assert data["soul_md"] == "doomed soul"
        assert not (agents_dir / "wt-delete-me").exists()
        assert client.get("/agents/wt-delete-me").status_code == 404

    def test_delete_missing_404(self, gateway_session):
        client, _, _ = gateway_session
        response = client.delete("/agents/wt-never-existed")
        assert response.status_code == 404


class TestSecretRedaction:
    def test_no_operator_token_in_any_agent_response(self, gateway_session):
        client, operator_token, _ = gateway_session
        for path in ["/agents", "/providers", "/agents/sre-triage"]:
            assert operator_token not in client.get(path).content.decode()
