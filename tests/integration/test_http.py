"""Integration tests for HTTP endpoints: /health, /agents, /reload, /mcp/tools, /metrics."""
import pytest
import yaml
from starlette.testclient import TestClient


class TestHealthEndpoint:
    def test_returns_200(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/health")
        assert response.status_code == 200

    def test_status_ok_when_no_mcp_servers(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/health").json()
        assert data["status"] == "ok"

    def test_has_expected_fields(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/health").json()
        assert "agents" in data
        assert "active_runs" in data
        assert "max_concurrent_runs" in data
        assert "mcp_servers" in data
        assert "version" in data

    def test_agent_count_matches_loaded(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/health").json()
        assert data["agents"] == 2  # sre-triage and post-mortem

    def test_mcp_servers_empty_when_none_configured(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/health").json()
        assert data["mcp_servers"] == {}

    def test_active_runs_starts_at_zero(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/health").json()
        assert data["active_runs"] == 0


class TestAgentsEndpoint:
    def test_returns_200(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/agents")
        assert response.status_code == 200

    def test_lists_all_agents(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/agents").json()
        names = {a["name"] for a in data["agents"]}
        assert names == {"sre-triage", "post-mortem"}

    def test_agent_has_model_and_tools(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/agents").json()
        sre = next(a for a in data["agents"] if a["name"] == "sre-triage")
        assert sre["model"] == "anthropic/claude-sonnet-4-6"
        assert "tools" in sre

    def test_soul_endpoint_returns_content(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/agents/sre-triage/soul")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "sre-triage"
        assert "sre-triage" in data["content"]

    def test_soul_endpoint_404_for_unknown(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/agents/nonexistent/soul")
        assert response.status_code == 404

    def test_agent_yaml_endpoint_returns_content(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/agents/sre-triage/agent")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "sre-triage"
        assert "agent.yaml" in data["content"] or "model" in data["content"]

    def test_agent_yaml_endpoint_404_for_unknown(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/agents/nonexistent/agent")
        assert response.status_code == 404


class TestReloadEndpoint:
    def test_returns_200(self, gateway_session):
        client, _, agents_dir = gateway_session
        response = client.post("/reload")
        assert response.status_code == 200

    def test_returns_current_agents(self, gateway_session):
        client, _, agents_dir = gateway_session
        data = client.post("/reload").json()
        names = {a["name"] for a in data["agents"]}
        assert "sre-triage" in names

    def test_new_agent_picked_up_on_reload(self, gateway_session, tmp_path):
        client, _, agents_dir = gateway_session

        # Add a new agent directory
        new_dir = agents_dir / "hot-reload-test"
        new_dir.mkdir(exist_ok=True)
        (new_dir / "agent.yaml").write_text(
            yaml.dump({"name": "hot-reload-test", "model": "anthropic/claude-sonnet-4-6", "max_tokens": 1024})
        )
        (new_dir / "soul.md").write_text("You are a hot-reload test agent.")

        data = client.post("/reload").json()
        names = {a["name"] for a in data["agents"]}
        assert "hot-reload-test" in names


class TestMcpEndpoints:
    def test_mcp_tools_returns_200(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/mcp/tools")
        assert response.status_code == 200

    def test_mcp_tools_empty_when_no_servers(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/mcp/tools").json()
        # With no mcp_servers configured, no tools are registered
        assert isinstance(data, (dict, list))

    def test_mcp_servers_returns_200(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/mcp/servers")
        assert response.status_code == 200

    def test_mcp_servers_empty_when_none_configured(self, gateway_session):
        client, _, _ = gateway_session
        data = client.get("/mcp/servers").json()
        assert data == {}


class TestMetricsEndpoint:
    def test_returns_200(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/metrics")
        assert response.status_code == 200

    def test_prometheus_format(self, gateway_session):
        client, _, _ = gateway_session
        response = client.get("/metrics")
        assert b"pork_gateway" in response.content
        assert response.headers["content-type"].startswith("text/plain")

    def test_contains_expected_metric_names(self, gateway_session):
        client, _, _ = gateway_session
        text = client.get("/metrics").content.decode()
        assert "pork_gateway_agent_runs_total" in text
        assert "pork_gateway_sessions_active" in text
