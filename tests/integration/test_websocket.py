"""Integration tests for the /rpc WebSocket endpoint."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.llm.providers.base import ProviderResponse
from gateway.runner.agent_runner import AgentRunResult


def _text_result(text="Test response"):
    return AgentRunResult(
        text=text,
        model_used="claude-sonnet-4-6",
        provider="anthropic",
        duration_ms=42,
        tool_calls_made=0,
        iterations=1,
        usage={"input_tokens": 100, "output_tokens": 20},
        trace=[
            {"type": "llm_call", "iteration": 1},
            {"type": "text", "content": text},
        ],
    )


class TestAuthFlow:
    def test_challenge_sent_on_connect(self, gateway_session):
        client, _, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            frame = ws.receive_json()
            assert frame["type"] == "event"
            assert frame["event"] == "challenge"
            assert "nonce" in frame["payload"]

    def test_invalid_token_rejected(self, gateway_session):
        client, _, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            ws.receive_json()  # challenge
            ws.send_json({
                "type": "req",
                "id": "1",
                "method": "connect",
                "params": {"auth": {"token": "wrong-token"}},
            })
            res = ws.receive_json()
            assert res["ok"] is False
            assert "token" in res["error"]["message"].lower()

    def test_valid_token_accepted(self, gateway_session):
        client, token, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            ws.receive_json()  # challenge
            ws.send_json({
                "type": "req",
                "id": "1",
                "method": "connect",
                "params": {"auth": {"token": token}},
            })
            res = ws.receive_json()
            assert res["ok"] is True
            assert "protocol" in res["payload"]

    def test_unauthenticated_agent_call_rejected(self, gateway_session):
        client, _, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            ws.receive_json()  # challenge
            ws.send_json({
                "type": "req",
                "id": "2",
                "method": "agent",
                "params": {"agentId": "sre-triage", "message": "hello"},
            })
            res = ws.receive_json()
            assert res["ok"] is False
            assert "not authenticated" in res["error"]["message"].lower()

    def test_unknown_method_before_auth_rejected(self, gateway_session):
        client, _, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            ws.receive_json()  # challenge
            ws.send_json({
                "type": "req",
                "id": "x",
                "method": "unknown",
                "params": {},
            })
            res = ws.receive_json()
            assert res["ok"] is False


def _auth(ws, token):
    """Authenticate a connected WebSocket client."""
    ws.receive_json()  # challenge
    ws.send_json({
        "type": "req",
        "id": "auth",
        "method": "connect",
        "params": {"auth": {"token": token}},
    })
    res = ws.receive_json()
    assert res["ok"] is True, f"Auth failed: {res}"


class TestAgentCall:
    def test_unknown_agent_returns_error(self, gateway_session):
        client, token, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            _auth(ws, token)
            ws.send_json({
                "type": "req",
                "id": "2",
                "method": "agent",
                "params": {
                    "agentId": "nonexistent-agent",
                    "sessionKey": "agent:nonexistent-agent:run-1",
                    "message": "hello",
                },
            })
            res = ws.receive_json()
            assert res["ok"] is False
            assert "not found" in res["error"]["message"].lower()

    def test_invalid_session_key_returns_error(self, gateway_session):
        client, token, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            _auth(ws, token)
            ws.send_json({
                "type": "req",
                "id": "3",
                "method": "agent",
                "params": {
                    "agentId": "sre-triage",
                    "sessionKey": "wrong-prefix:run-1",
                    "message": "hello",
                },
            })
            res = ws.receive_json()
            assert res["ok"] is False
            assert "sessionkey" in res["error"]["message"].lower()

    def test_successful_agent_run(self, gateway_session, monkeypatch):
        """Full happy path: accepted frame + trace frames + final ok frame."""
        client, token, _ = gateway_session

        mock_result = _text_result("Analysis complete")
        mock_run = AsyncMock(return_value=mock_result)

        from gateway.main import _state
        original_runner = _state["agent_runner"]
        _state["agent_runner"].run = mock_run

        try:
            with client.websocket_connect("/rpc") as ws:
                _auth(ws, token)
                ws.send_json({
                    "type": "req",
                    "id": "run-1",
                    "method": "agent",
                    "params": {
                        "agentId": "sre-triage",
                        "sessionKey": "agent:sre-triage:run-abc:step-1",
                        "message": "analyze the alert",
                    },
                })

                # Frame 1: accepted
                accepted = ws.receive_json()
                assert accepted["ok"] is True
                assert accepted["payload"]["status"] == "accepted"
                assert "runId" in accepted["payload"]

                # Drain trace events until we get the final result
                frames = []
                while True:
                    frame = ws.receive_json()
                    frames.append(frame)
                    payload = frame.get("payload", {})
                    if payload.get("status") in ("ok", "error") or not frame.get("ok"):
                        break

                # Find final ok frame
                final = frames[-1]
                assert final["ok"] is True
                assert final["payload"]["status"] == "ok"
                result = final["payload"]["result"]
                assert result["payloads"][0]["text"] == "Analysis complete"
                assert "meta" in result
                assert "trace" in result
        finally:
            _state["agent_runner"].run = original_runner.run

    def test_agent_run_error_returns_not_ok(self, gateway_session):
        client, token, _ = gateway_session

        from gateway.runner.agent_runner import AgentRunError
        mock_run = AsyncMock(side_effect=AgentRunError("All models exhausted"))

        from gateway.main import _state
        _state["agent_runner"].run = mock_run

        try:
            with client.websocket_connect("/rpc") as ws:
                _auth(ws, token)
                ws.send_json({
                    "type": "req",
                    "id": "run-err",
                    "method": "agent",
                    "params": {
                        "agentId": "sre-triage",
                        "sessionKey": "agent:sre-triage:run-err:step-1",
                        "message": "fail",
                    },
                })

                # accepted frame first
                accepted = ws.receive_json()
                assert accepted["payload"]["status"] == "accepted"

                # Error frame
                frames = []
                while True:
                    frame = ws.receive_json()
                    frames.append(frame)
                    payload = frame.get("payload", {})
                    if not frame.get("ok") or payload.get("status") in ("ok",):
                        break

                error_frame = frames[-1]
                assert error_frame["ok"] is False
                assert "exhausted" in error_frame["error"]["message"].lower()
        finally:
            from gateway.runner.agent_runner import AgentRunner
            _state["agent_runner"].run = AgentRunner.run.__get__(
                _state["agent_runner"], AgentRunner
            )

    def test_unknown_method_after_auth_returns_error(self, gateway_session):
        client, token, _ = gateway_session
        with client.websocket_connect("/rpc") as ws:
            _auth(ws, token)
            ws.send_json({
                "type": "req",
                "id": "9",
                "method": "unknown_method",
                "params": {},
            })
            res = ws.receive_json()
            assert res["ok"] is False
            assert "unknown method" in res["error"]["message"].lower()

    def test_non_req_type_messages_ignored(self, gateway_session):
        client, token, _ = gateway_session

        mock_result = _text_result("ok")
        mock_run = AsyncMock(return_value=mock_result)

        from gateway.main import _state
        _state["agent_runner"].run = mock_run

        try:
            with client.websocket_connect("/rpc") as ws:
                _auth(ws, token)

                # Send a non-req type — should be silently ignored
                ws.send_json({"type": "notification", "event": "ping"})

                # Now send a real req — should still work
                ws.send_json({
                    "type": "req",
                    "id": "run-after-notification",
                    "method": "agent",
                    "params": {
                        "agentId": "sre-triage",
                        "sessionKey": "agent:sre-triage:run-x:step-1",
                        "message": "go",
                    },
                })
                accepted = ws.receive_json()
                assert accepted["payload"]["status"] == "accepted"
        finally:
            from gateway.runner.agent_runner import AgentRunner
            _state["agent_runner"].run = AgentRunner.run.__get__(
                _state["agent_runner"], AgentRunner
            )
