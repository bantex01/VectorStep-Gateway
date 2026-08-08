"""Gateway tool-call policy — operator-owned allow/deny at the tool boundary.

See SPEC-gateway-tool-policy.md for the design. Pure/no-IO: everything here
operates on already-loaded config and in-memory call data.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gateway.models.config import ToolPolicyConfig, ToolPolicyMatch

if TYPE_CHECKING:
    from gateway.mcp.manager import MCPTool

logger = logging.getLogger(__name__)

# Bounds regex cost against pathological inputs — not a security boundary.
# Beyond this size a rule with input_regex is skipped (treated as "does not
# match") rather than paying to serialise/search arbitrarily large input.
_INPUT_SERIALISATION_CAP = 1_000_000


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str | None = None
    rule_index: int | None = None


class _CompiledRule:
    def __init__(self, index: int, action: str, match: ToolPolicyMatch, reason: str | None):
        self.index = index
        self.action = action  # "allow" | "deny"
        self.reason = reason
        self.server = match.server
        self._tool_glob = match.tool
        self._agent_glob = match.agent
        self._input_regex = re.compile(match.input_regex) if match.input_regex else None

    def matches_static(self, agent_name: str, server: str, tool: str) -> bool:
        """server/tool/agent match only — used both as the first stage of
        `matches()` and, standalone, for visibility filtering (which can never
        know a call's input ahead of time)."""
        if self.server is not None and self.server != server:
            return False
        if self._tool_glob is not None and not fnmatch.fnmatch(tool, self._tool_glob):
            return False
        if self._agent_glob is not None and not fnmatch.fnmatch(agent_name, self._agent_glob):
            return False
        return True

    def matches(self, agent_name: str, server: str, tool: str, tool_input: dict, warn_oversized) -> bool:
        if not self.matches_static(agent_name, server, tool):
            return False
        if self._input_regex is not None:
            serialised = json.dumps(tool_input, sort_keys=True)
            if len(serialised) > _INPUT_SERIALISATION_CAP:
                warn_oversized(tool)
                return False
            if not self._input_regex.search(serialised):
                return False
        return True


class ToolPolicy:
    """Compiled tool_policy config. Construct once at startup; `evaluate()` and
    `visible_tools()` are called on every tool call / tool-listing respectively."""

    def __init__(self, config: ToolPolicyConfig):
        self._default_allow = config.default == "allow"
        self._rules: list[_CompiledRule] = [
            _CompiledRule(
                index=i,
                action="allow" if rule.allow is not None else "deny",
                match=rule.allow if rule.allow is not None else rule.deny,
                reason=rule.reason,
            )
            for i, rule in enumerate(config.rules)
        ]
        self._warned_oversized = False

    def _warn_oversized(self, tool: str) -> None:
        if not self._warned_oversized:
            self._warned_oversized = True
            logger.warning(
                "tool_policy: input for tool '%s' exceeds the %d-byte serialisation "
                "cap for input_regex matching — skipping regex rules for this and any "
                "subsequent oversized call this run (not logged again)",
                tool, _INPUT_SERIALISATION_CAP,
            )

    def evaluate(self, agent_name: str, server: str, tool: str, tool_input: dict) -> PolicyDecision:
        for rule in self._rules:
            if rule.matches(agent_name, server, tool, tool_input, self._warn_oversized):
                return PolicyDecision(
                    allowed=rule.action == "allow",
                    reason=rule.reason,
                    rule_index=rule.index,
                )
        return PolicyDecision(allowed=self._default_allow)

    def visible_tools(self, agent_name: str, tools: list["MCPTool"]) -> list["MCPTool"]:
        """Filter tools presented to the LLM. Under default-allow this is a
        no-op — denials happen at call time. Under default-deny, a tool is
        hidden only when *no* allow rule could ever reach it for this agent
        (conservative: any potentially-matching allow rule keeps it visible,
        input_regex is ignored here since it can't be evaluated without a call)."""
        if self._default_allow:
            return tools
        return [
            tool for tool in tools
            if any(
                rule.action == "allow" and rule.matches_static(agent_name, tool.server_name, tool.name)
                for rule in self._rules
            )
        ]
