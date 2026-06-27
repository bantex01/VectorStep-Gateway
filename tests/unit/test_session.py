"""Tests for SessionManager."""
from gateway.session.manager import Session, SessionManager


class TestSessionManager:
    def test_creates_new_session(self):
        mgr = SessionManager()
        session = mgr.get_or_create("agent:sre:run-1:triage", "sre")
        assert isinstance(session, Session)
        assert session.session_key == "agent:sre:run-1:triage"
        assert session.agent_name == "sre"
        assert session.messages == []

    def test_returns_same_session_on_second_call(self):
        mgr = SessionManager()
        s1 = mgr.get_or_create("agent:sre:run-1:triage", "sre")
        s2 = mgr.get_or_create("agent:sre:run-1:triage", "sre")
        assert s1 is s2

    def test_different_keys_give_different_sessions(self):
        mgr = SessionManager()
        s1 = mgr.get_or_create("agent:sre:run-1:step-a", "sre")
        s2 = mgr.get_or_create("agent:sre:run-1:step-b", "sre")
        assert s1 is not s2

    def test_session_count_increments(self):
        mgr = SessionManager()
        mgr.get_or_create("agent:a:run-1", "a")
        mgr.get_or_create("agent:b:run-2", "b")
        assert len(mgr._sessions) == 2

    def test_session_count_stable_on_existing_key(self):
        mgr = SessionManager()
        mgr.get_or_create("agent:a:run-1", "a")
        mgr.get_or_create("agent:a:run-1", "a")
        assert len(mgr._sessions) == 1

    def test_session_messages_field_starts_empty(self):
        mgr = SessionManager()
        session = mgr.get_or_create("agent:x:run-1", "x")
        assert session.messages == []


class TestValidateKey:
    def test_valid_key_returns_none(self):
        mgr = SessionManager()
        assert mgr.validate_key("agent:sre-triage:run-1:step", "sre-triage") is None

    def test_missing_agent_prefix_returns_error(self):
        mgr = SessionManager()
        error = mgr.validate_key("pipeline:run-1:step", "sre-triage")
        assert error is not None
        assert "sre-triage" in error

    def test_wrong_agent_prefix_returns_error(self):
        mgr = SessionManager()
        error = mgr.validate_key("agent:other-agent:run-1", "sre-triage")
        assert error is not None
        assert "sre-triage" in error

    def test_exact_prefix_valid(self):
        mgr = SessionManager()
        # Minimum valid key: agent:<agentId>: followed by anything
        assert mgr.validate_key("agent:my-agent:x", "my-agent") is None

    def test_bare_key_no_prefix_returns_error(self):
        mgr = SessionManager()
        error = mgr.validate_key("just-a-key", "sre-triage")
        assert error is not None
