"""Tests for the gateway tool-call policy — pure ToolPolicy logic (no IO)."""
import logging

import pytest
from pydantic import ValidationError

from gateway.mcp.manager import MCPTool
from gateway.models.config import ToolPolicyConfig, ToolPolicyMatch, ToolPolicyRule
from gateway.policy import ToolPolicy, _INPUT_SERIALISATION_CAP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(action: str, reason: str | None = None, **match_fields) -> ToolPolicyRule:
    match = ToolPolicyMatch(**match_fields)
    kwargs = {action: match}
    if reason is not None:
        kwargs["reason"] = reason
    return ToolPolicyRule(**kwargs)


def _tool(name: str, server: str = "atlassian") -> MCPTool:
    return MCPTool(
        name=name,
        registered_name=f"{server}__{name}",
        description="",
        input_schema={},
        server_name=server,
    )


# ---------------------------------------------------------------------------
# Config model validation
# ---------------------------------------------------------------------------


def test_unknown_match_key_is_validation_error():
    with pytest.raises(ValidationError):
        ToolPolicyMatch(sever="atlassian")  # typo — must fail closed, not match nothing silently


def test_deny_without_reason_is_validation_error():
    with pytest.raises(ValidationError):
        ToolPolicyRule(deny=ToolPolicyMatch(tool="jira_delete_issue"))


def test_deny_with_reason_is_valid():
    ToolPolicyRule(deny=ToolPolicyMatch(tool="jira_delete_issue"), reason="destructive")


def test_allow_without_reason_is_valid():
    ToolPolicyRule(allow=ToolPolicyMatch(server="grafana"))


def test_rule_with_neither_allow_nor_deny_is_validation_error():
    with pytest.raises(ValidationError):
        ToolPolicyRule(reason="orphaned reason")


def test_rule_with_both_allow_and_deny_is_validation_error():
    with pytest.raises(ValidationError):
        ToolPolicyRule(
            allow=ToolPolicyMatch(server="grafana"),
            deny=ToolPolicyMatch(server="grafana"),
            reason="ambiguous",
        )


def test_require_approval_is_rejected_as_not_yet_supported():
    with pytest.raises(ValidationError, match="not yet supported"):
        ToolPolicyRule(require_approval=ToolPolicyMatch(tool="risky_tool"))


# ---------------------------------------------------------------------------
# evaluate() — precedence, AND semantics, matching
# ---------------------------------------------------------------------------


def test_default_allow_with_no_rules():
    policy = ToolPolicy(ToolPolicyConfig(default="allow", rules=[]))
    decision = policy.evaluate("any-agent", "atlassian", "jira_search", {})
    assert decision.allowed
    assert decision.rule_index is None


def test_default_deny_with_no_rules():
    policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[]))
    decision = policy.evaluate("any-agent", "atlassian", "jira_search", {})
    assert not decision.allowed


def test_first_match_wins_deny_then_allow():
    policy = ToolPolicy(ToolPolicyConfig(rules=[
        _rule("deny", reason="blocked", tool="jira_delete_issue"),
        _rule("allow", server="atlassian"),
    ]))
    decision = policy.evaluate("agent", "atlassian", "jira_delete_issue", {})
    assert not decision.allowed
    assert decision.rule_index == 0
    assert decision.reason == "blocked"


def test_first_match_wins_allow_then_deny():
    policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[
        _rule("allow", server="atlassian"),
        _rule("deny", reason="blocked", tool="jira_delete_issue"),
    ]))
    decision = policy.evaluate("agent", "atlassian", "jira_delete_issue", {})
    assert decision.allowed
    assert decision.rule_index == 0


def test_and_semantics_within_match_block():
    policy = ToolPolicy(ToolPolicyConfig(rules=[
        _rule("deny", reason="blocked", server="atlassian", tool="jira_delete_issue"),
    ]))
    # server matches but tool doesn't -> rule doesn't fire, falls to default allow
    decision = policy.evaluate("agent", "atlassian", "jira_search", {})
    assert decision.allowed
    # both match -> rule fires
    decision = policy.evaluate("agent", "atlassian", "jira_delete_issue", {})
    assert not decision.allowed


def test_tool_glob_matching():
    policy = ToolPolicy(ToolPolicyConfig(rules=[
        _rule("deny", reason="mutating", tool="execute_*"),
    ]))
    assert not policy.evaluate("agent", "grafana", "execute_promql", {}).allowed
    assert policy.evaluate("agent", "grafana", "query_promql", {}).allowed


def test_agent_glob_scoping():
    policy = ToolPolicy(ToolPolicyConfig(rules=[
        _rule("deny", reason="toolless in this deployment", agent="experimental-*"),
    ]))
    assert not policy.evaluate("experimental-foo", "grafana", "query", {}).allowed
    assert policy.evaluate("prod-foo", "grafana", "query", {}).allowed


def test_input_regex_matching():
    policy = ToolPolicy(ToolPolicyConfig(rules=[
        _rule("deny", reason="mutating queries blocked", tool="execute_*", input_regex="(?i)delete|drop"),
    ]))
    assert not policy.evaluate("agent", "grafana", "execute_promql", {"query": "DELETE FROM x"}).allowed
    assert policy.evaluate("agent", "grafana", "execute_promql", {"query": "select 1"}).allowed


def test_input_regex_serialisation_cap_skips_rule_and_warns_once(caplog):
    policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[
        _rule("allow", server="grafana", input_regex=".*"),
    ]))
    oversized = {"query": "x" * (_INPUT_SERIALISATION_CAP + 1)}

    with caplog.at_level(logging.WARNING):
        decision1 = policy.evaluate("agent", "grafana", "query", oversized)
        decision2 = policy.evaluate("agent", "grafana", "query", oversized)

    # Rule skipped (treated as non-match) both times -> falls through to default deny.
    assert not decision1.allowed
    assert not decision2.allowed
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # logged once per run, not once per call


# ---------------------------------------------------------------------------
# visible_tools() — default-deny visibility filtering
# ---------------------------------------------------------------------------


def test_visibility_unfiltered_under_default_allow():
    policy = ToolPolicy(ToolPolicyConfig(default="allow", rules=[
        _rule("deny", reason="blocked", tool="jira_delete_issue"),
    ]))
    tools = [_tool("jira_delete_issue"), _tool("jira_search")]
    assert policy.visible_tools("agent", tools) == tools


def test_visibility_filtered_under_default_deny():
    policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[
        _rule("allow", server="atlassian", tool="jira_search"),
    ]))
    tools = [_tool("jira_search"), _tool("jira_delete_issue")]
    visible = policy.visible_tools("agent", tools)
    assert [t.name for t in visible] == ["jira_search"]


def test_visibility_agent_scoping_under_default_deny():
    policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[
        _rule("allow", server="atlassian", agent="prod-*"),
    ]))
    tools = [_tool("jira_search")]
    assert policy.visible_tools("prod-triage", tools) == tools
    assert policy.visible_tools("experimental-triage", tools) == []


def test_input_regex_never_hides_under_default_deny():
    # An allow rule with input_regex is "callable-in-principle" — visibility is
    # conservative and keeps it visible even though input_regex can't be
    # evaluated without an actual call.
    policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[
        _rule("allow", server="grafana", tool="execute_promql", input_regex="^safe$"),
    ]))
    tools = [_tool("execute_promql", server="grafana")]
    assert policy.visible_tools("agent", tools) == tools


def test_deny_rule_does_not_affect_visibility():
    # No allow rule at all under default-deny -> hidden, regardless of any deny rules present.
    policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[
        _rule("deny", reason="blocked", tool="jira_delete_issue"),
    ]))
    tools = [_tool("jira_search")]
    assert policy.visible_tools("agent", tools) == []
