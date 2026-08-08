"""Tests for AgentRunner — the core agentic loop with retry, fallback, and tool execution."""
import asyncio

import pytest

from prometheus_client import REGISTRY

from gateway.llm.providers.base import BaseProvider, ProviderError, ProviderResponse
from gateway.mcp.manager import MCPTool, MCPToolResult
from gateway.models.agent import AgentConfig
from gateway.models.config import LimitsConfig, ToolPolicyConfig, ToolPolicyMatch, ToolPolicyRule
from gateway.policy import ToolPolicy
from gateway.runner.agent_runner import AgentRunError, AgentRunner, _dedupe_preserve_order


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeProvider(BaseProvider):
    """Returns responses from a pre-loaded queue; raises if the queue is exhausted."""

    def __init__(self, responses: list):
        self._queue = list(responses)

    async def complete(self, system, messages, tools, model, max_tokens, thinking_level=None):
        if not self._queue:
            raise AssertionError("FakeProvider queue exhausted — unexpected extra call to complete()")
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeRouter:
    """Routes every model string to the same (or model-mapped) provider."""

    def __init__(self, provider=None, by_model: dict | None = None):
        self._default = provider
        self._by_model = by_model or {}

    def get_provider_and_model(self, model_string: str):
        provider = self._by_model.get(model_string, self._default)
        bare = model_string.split("/", 1)[-1] if "/" in model_string else model_string
        return provider, bare


class FakeMCPManager:
    """MCP manager test double — returns predetermined tool results."""

    def __init__(self, tool_results: dict | None = None, tools: list | None = None):
        self._tools = tools or []
        self._tool_results = tool_results or {}

    def get_tools_for_agent(self, agent):
        return self._tools

    def get_server_for_tool(self, name: str) -> str:
        return "test-server"

    async def call_tool(self, name: str, arguments: dict) -> MCPToolResult:
        if name in self._tool_results:
            return self._tool_results[name]
        return MCPToolResult(content=[{"type": "text", "text": f"{name}_result"}], is_error=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent(model="anthropic/claude-sonnet-4-6", fallbacks=None):
    return AgentConfig(
        name="test-agent",
        model=model,
        max_tokens=1024,
        model_fallbacks=fallbacks or [],
        soul="You are a test agent.",
    )


def _limits(retry_attempts=0, retry_delay=0.0, max_iterations=5):
    return LimitsConfig(
        llm_retry_attempts=retry_attempts,
        llm_retry_base_delay_seconds=retry_delay,
        max_agent_iterations=max_iterations,
        request_timeout_seconds=10,
        mcp_tool_timeout_seconds=5,
        max_concurrent_runs=10,
    )


def _text_response(text="Agent response", stop_reason="end_turn", model="claude-sonnet-4-6"):
    return ProviderResponse(
        content_blocks=[{"type": "text", "text": text}],
        stop_reason=stop_reason,
        model_used=model,
        usage={"input_tokens": 100, "output_tokens": 20},
    )


def _tool_response(tool_name="my__tool", tool_id="call_1", args=None):
    return ProviderResponse(
        content_blocks=[{
            "type": "tool_use",
            "id": tool_id,
            "name": tool_name,
            "input": args or {"query": "test"},
        }],
        stop_reason="tool_use",
        model_used="claude-sonnet-4-6",
        usage={"input_tokens": 80, "output_tokens": 10},
    )


async def _run(
    runner, agent, mcp=None, model_override=None, limits=None,
    trace_tool_result_max=None, tool_policy=None,
):
    """Convenience wrapper: run and collect all trace events."""
    events = []

    async def on_event(e):
        events.append(e)

    result = await runner.run(
        agent=agent,
        session_key=f"agent:{agent.name}:test-run",
        message="test message",
        model_override=model_override,
        thinking_level=None,
        mcp_manager=mcp or FakeMCPManager(),
        limits=limits or _limits(),
        tool_policy=tool_policy,
        on_trace_event=on_event,
        trace_tool_result_max=trace_tool_result_max,
    )
    return result, events


# ---------------------------------------------------------------------------
# _dedupe_preserve_order
# ---------------------------------------------------------------------------


class TestDedupePreserveOrder:
    def test_empty(self):
        assert _dedupe_preserve_order([]) == []

    def test_no_dupes(self):
        assert _dedupe_preserve_order(["a", "b", "c"]) == ["a", "b", "c"]

    def test_removes_duplicates(self):
        assert _dedupe_preserve_order(["a", "b", "a", "c"]) == ["a", "b", "c"]

    def test_preserves_first_occurrence_order(self):
        result = _dedupe_preserve_order(["b", "a", "b", "c", "a"])
        assert result == ["b", "a", "c"]

    def test_all_same(self):
        assert _dedupe_preserve_order(["x", "x", "x"]) == ["x"]


# ---------------------------------------------------------------------------
# Single turn, no tools
# ---------------------------------------------------------------------------


class TestSimpleTextResponse:
    async def test_returns_agent_run_result(self):
        provider = FakeProvider([_text_response("Hello world")])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent())

        assert result.text == "Hello world"
        assert result.iterations == 1
        assert result.tool_calls_made == 0
        assert result.model_used == "claude-sonnet-4-6"

    async def test_usage_accumulated(self):
        provider = FakeProvider([_text_response()])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent())

        assert result.usage["input_tokens"] == 100
        assert result.usage["output_tokens"] == 20

    async def test_result_carries_agent_version(self):
        provider = FakeProvider([_text_response()])
        runner = AgentRunner(FakeRouter(provider))
        agent = _agent()
        agent.version = "abc123def456"
        result, _ = await _run(runner, agent)

        assert result.agent_version == "abc123def456"

    async def test_model_override_used(self):
        provider = FakeProvider([_text_response(model="claude-haiku-4-5")])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent(), model_override="anthropic/claude-haiku-4-5")
        assert result.model_used == "claude-haiku-4-5"

    async def test_trace_contains_llm_call_and_text_events(self):
        provider = FakeProvider([_text_response("hi")])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent())

        types = [e["type"] for e in events]
        assert "llm_call" in types
        assert "text" in types
        assert types.index("llm_call") < types.index("text")

    async def test_llm_call_event_has_iteration(self):
        provider = FakeProvider([_text_response()])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent())

        llm_events = [e for e in events if e["type"] == "llm_call"]
        assert llm_events[0]["iteration"] == 1

    async def test_text_event_content(self):
        provider = FakeProvider([_text_response("My response")])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent())

        text_events = [e for e in events if e["type"] == "text"]
        assert text_events[0]["content"] == "My response"

    async def test_multiple_text_blocks_joined(self):
        response = ProviderResponse(
            content_blocks=[
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": "Part 2"},
            ],
            stop_reason="end_turn",
            model_used="claude-sonnet-4-6",
            usage={"input_tokens": 50, "output_tokens": 10},
        )
        provider = FakeProvider([response])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent())
        assert result.text == "Part 1\nPart 2"

    async def test_thinking_block_emits_event(self):
        response = ProviderResponse(
            content_blocks=[
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "Answer"},
            ],
            stop_reason="end_turn",
            model_used="claude-sonnet-4-6",
            usage={"input_tokens": 50, "output_tokens": 10},
        )
        provider = FakeProvider([response])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent())

        thinking_events = [e for e in events if e["type"] == "thinking"]
        assert len(thinking_events) == 1
        assert thinking_events[0]["content"] == "Let me think..."


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


class TestToolCalls:
    async def test_single_tool_call_then_final_response(self):
        provider = FakeProvider([
            _tool_response("grafana__query", "call_1"),
            _text_response("Final answer after tool"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, events = await _run(runner, _agent(), mcp=FakeMCPManager())

        assert result.text == "Final answer after tool"
        assert result.iterations == 2
        assert result.tool_calls_made == 1

    async def test_trace_events_order_for_tool_call(self):
        provider = FakeProvider([
            _tool_response("grafana__query", "call_1"),
            _text_response("Done"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent(), mcp=FakeMCPManager())

        types = [e["type"] for e in events]
        assert types.index("tool_call") > types.index("llm_call")
        assert types.index("tool_result") > types.index("tool_call")
        llm_call_indices = [i for i, t in enumerate(types) if t == "llm_call"]
        assert llm_call_indices[1] > types.index("tool_result")

    async def test_tool_call_event_has_name_and_input(self):
        provider = FakeProvider([
            _tool_response("my__tool", "call_x", args={"key": "value"}),
            _text_response(),
        ])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent(), mcp=FakeMCPManager())

        tool_call_events = [e for e in events if e["type"] == "tool_call"]
        assert tool_call_events[0]["name"] == "my__tool"
        assert tool_call_events[0]["input"] == {"key": "value"}

    async def test_tool_result_event_content(self):
        mcp = FakeMCPManager(tool_results={
            "my__tool": MCPToolResult(
                content=[{"type": "text", "text": "tool output"}],
                is_error=False,
            )
        })
        provider = FakeProvider([
            _tool_response("my__tool"),
            _text_response(),
        ])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent(), mcp=mcp)

        result_events = [e for e in events if e["type"] == "tool_result"]
        assert result_events[0]["name"] == "my__tool"
        assert result_events[0]["content"] == "tool output"
        assert result_events[0]["is_error"] is False

    async def test_parallel_tool_calls_both_executed(self):
        call_log = []

        class TrackingMCP(FakeMCPManager):
            async def call_tool(self, name, arguments):
                call_log.append(name)
                return MCPToolResult(
                    content=[{"type": "text", "text": f"{name}_done"}],
                    is_error=False,
                )

        two_tools_response = ProviderResponse(
            content_blocks=[
                {"type": "tool_use", "id": "c1", "name": "tool_a", "input": {}},
                {"type": "tool_use", "id": "c2", "name": "tool_b", "input": {}},
            ],
            stop_reason="tool_use",
            model_used="claude-sonnet-4-6",
            usage={"input_tokens": 50, "output_tokens": 5},
        )
        provider = FakeProvider([two_tools_response, _text_response("Both done")])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent(), mcp=TrackingMCP())

        assert result.tool_calls_made == 2
        assert set(call_log) == {"tool_a", "tool_b"}

    async def test_tool_error_result_continues_run(self):
        mcp = FakeMCPManager(tool_results={
            "bad__tool": MCPToolResult(
                content=[{"type": "text", "text": "connection refused"}],
                is_error=True,
            )
        })
        provider = FakeProvider([
            _tool_response("bad__tool"),
            _text_response("Handled the error"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, events = await _run(runner, _agent(), mcp=mcp)

        assert result.text == "Handled the error"
        result_events = [e for e in events if e["type"] == "tool_result"]
        assert result_events[0]["is_error"] is True

    async def test_tool_result_truncated_in_trace(self):
        long_text = "x" * 5000
        mcp = FakeMCPManager(tool_results={
            "big__tool": MCPToolResult(
                content=[{"type": "text", "text": long_text}],
                is_error=False,
            )
        })
        provider = FakeProvider([_tool_response("big__tool"), _text_response()])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(runner, _agent(), mcp=mcp)

        result_events = [e for e in events if e["type"] == "tool_result"]
        assert len(result_events[0]["content"]) <= 3100
        assert "truncated" in result_events[0]["content"]

    async def test_tool_result_truncation_honours_limits_config_default(self):
        long_text = "x" * 500
        mcp = FakeMCPManager(tool_results={
            "big__tool": MCPToolResult(content=[{"type": "text", "text": long_text}], is_error=False)
        })
        provider = FakeProvider([_tool_response("big__tool"), _text_response()])
        runner = AgentRunner(FakeRouter(provider))
        limits = _limits()
        limits.trace_tool_result_max_chars = 100

        _, events = await _run(runner, _agent(), mcp=mcp, limits=limits)

        result_events = [e for e in events if e["type"] == "tool_result"]
        assert len(result_events[0]["content"]) <= 120
        assert "truncated" in result_events[0]["content"]

    async def test_tool_result_truncation_per_request_override_wins_over_limits_default(self):
        long_text = "x" * 5000
        mcp = FakeMCPManager(tool_results={
            "big__tool": MCPToolResult(content=[{"type": "text", "text": long_text}], is_error=False)
        })
        provider = FakeProvider([_tool_response("big__tool"), _text_response()])
        runner = AgentRunner(FakeRouter(provider))

        _, events = await _run(runner, _agent(), mcp=mcp, trace_tool_result_max=8000)

        result_events = [e for e in events if e["type"] == "tool_result"]
        assert result_events[0]["content"] == long_text
        assert "truncated" not in result_events[0]["content"]

    async def test_llm_conversation_receives_full_untruncated_tool_result(self):
        """The truncation only affects the TRACE copy — the actual conversation the LLM
        reasons over must always see the full tool output, never the truncated one."""
        long_text = "x" * 5000
        mcp = FakeMCPManager(tool_results={
            "big__tool": MCPToolResult(content=[{"type": "text", "text": long_text}], is_error=False)
        })
        seen_messages = []

        class _CapturingProvider(FakeProvider):
            async def complete(self, system, messages, tools, model, max_tokens, thinking_level=None):
                seen_messages.append(messages)
                return await super().complete(system, messages, tools, model, max_tokens, thinking_level)

        provider = _CapturingProvider([_tool_response("big__tool"), _text_response()])
        runner = AgentRunner(FakeRouter(provider))

        await _run(runner, _agent(), mcp=mcp)

        # The second complete() call includes the tool result appended to messages.
        final_messages = seen_messages[-1]
        serialised = str(final_messages)
        assert long_text in serialised

    async def test_usage_accumulates_across_iterations(self):
        provider = FakeProvider([
            ProviderResponse(
                content_blocks=[{"type": "tool_use", "id": "c1", "name": "t", "input": {}}],
                stop_reason="tool_use",
                model_used="claude-sonnet-4-6",
                usage={"input_tokens": 100, "output_tokens": 10},
            ),
            ProviderResponse(
                content_blocks=[{"type": "text", "text": "done"}],
                stop_reason="end_turn",
                model_used="claude-sonnet-4-6",
                usage={"input_tokens": 200, "output_tokens": 30},
            ),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent(), mcp=FakeMCPManager())

        assert result.usage["input_tokens"] == 300
        assert result.usage["output_tokens"] == 40

    async def test_openai_compat_tool_call_path(self):
        """openrouter/ prefix uses OpenAI-compat stop_reason 'tool_calls'."""
        provider = FakeProvider([
            ProviderResponse(
                content_blocks=[{"type": "tool_use", "id": "c1", "name": "t", "input": {}}],
                stop_reason="tool_calls",  # OpenAI-compat
                model_used="gpt-4",
                usage={"input_tokens": 50, "output_tokens": 5},
            ),
            _text_response("OpenAI done", model="gpt-4"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(
            runner,
            _agent(model="openrouter/openai/gpt-4"),
            mcp=FakeMCPManager(),
        )
        assert result.text == "OpenAI done"
        assert result.tool_calls_made == 1

    async def test_azure_prefix_uses_openai_compat_tool_path(self):
        """azure/ must be treated as OpenAI-compat — same tool_calls/tool_use_id path as openrouter/."""
        provider = FakeProvider([
            ProviderResponse(
                content_blocks=[{"type": "tool_use", "id": "c1", "name": "t", "input": {}}],
                stop_reason="tool_calls",  # OpenAI-compat
                model_used="azure/gpt-5",
                usage={"input_tokens": 50, "output_tokens": 5},
            ),
            _text_response("Azure done", model="azure/gpt-5"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(
            runner,
            _agent(model="azure/gpt-5"),
            mcp=FakeMCPManager(),
        )
        assert result.text == "Azure done"
        assert result.tool_calls_made == 1


# ---------------------------------------------------------------------------
# Tool policy — runner integration (pure ToolPolicy logic is in
# tests/unit/test_tool_policy.py; this covers the agent_runner.py seam)
# ---------------------------------------------------------------------------


def _deny_all_policy(reason="blocked by test policy") -> ToolPolicy:
    return ToolPolicy(ToolPolicyConfig(rules=[
        ToolPolicyRule(deny=ToolPolicyMatch(), reason=reason),
    ]))


class TestToolPolicy:
    async def test_denied_call_returns_error_result_and_loop_continues(self):
        provider = FakeProvider([
            _tool_response("atlassian__jira_delete_issue", "call_1"),
            _text_response("Reported that I couldn't do that"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, events = await _run(
            runner, _agent(), mcp=FakeMCPManager(), tool_policy=_deny_all_policy("nope"),
        )

        assert result.text == "Reported that I couldn't do that"
        assert result.iterations == 2  # LLM got the denial and kept going

    async def test_denied_call_emits_tool_denied_trace_event(self):
        provider = FakeProvider([
            _tool_response("atlassian__jira_delete_issue", "call_1"),
            _text_response("Done"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        _, events = await _run(
            runner, _agent(), mcp=FakeMCPManager(), tool_policy=_deny_all_policy("destructive op"),
        )

        denied = [e for e in events if e["type"] == "tool_denied"]
        assert len(denied) == 1
        assert denied[0]["name"] == "atlassian__jira_delete_issue"
        assert denied[0]["server"] == "atlassian"
        assert denied[0]["reason"] == "destructive op"
        assert denied[0]["rule_index"] == 0
        # No normal tool_result event for a denied call — the loop never
        # reaches mcp_manager.call_tool.
        assert not [e for e in events if e["type"] == "tool_result"]

    async def test_denied_call_never_reaches_mcp_manager(self):
        call_log = []

        class TrackingMCP(FakeMCPManager):
            async def call_tool(self, name, arguments):
                call_log.append(name)
                return await super().call_tool(name, arguments)

        provider = FakeProvider([
            _tool_response("atlassian__jira_delete_issue", "call_1"),
            _text_response("Done"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        await _run(runner, _agent(), mcp=TrackingMCP(), tool_policy=_deny_all_policy())

        assert call_log == []

    async def test_parallel_turn_one_denied_one_allowed(self):
        call_log = []

        class TrackingMCP(FakeMCPManager):
            async def call_tool(self, name, arguments):
                call_log.append(name)
                return MCPToolResult(content=[{"type": "text", "text": f"{name}_ok"}], is_error=False)

        two_tools_response = ProviderResponse(
            content_blocks=[
                {"type": "tool_use", "id": "c1", "name": "atlassian__jira_delete_issue", "input": {}},
                {"type": "tool_use", "id": "c2", "name": "grafana__query", "input": {}},
            ],
            stop_reason="tool_use",
            model_used="claude-sonnet-4-6",
            usage={"input_tokens": 50, "output_tokens": 5},
        )
        provider = FakeProvider([two_tools_response, _text_response("Both handled")])
        runner = AgentRunner(FakeRouter(provider))
        policy = ToolPolicy(ToolPolicyConfig(rules=[
            ToolPolicyRule(deny=ToolPolicyMatch(tool="jira_delete_issue"), reason="destructive"),
        ]))
        result, events = await _run(runner, _agent(), mcp=TrackingMCP(), tool_policy=policy)

        assert result.tool_calls_made == 2
        assert call_log == ["grafana__query"]  # only the allowed one actually dispatched
        denied = [e for e in events if e["type"] == "tool_denied"]
        assert [d["name"] for d in denied] == ["atlassian__jira_delete_issue"]

    async def test_denial_increments_metric(self):
        provider = FakeProvider([
            _tool_response("atlassian__jira_delete_issue", "call_1"),
            _text_response("Done"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        before = REGISTRY.get_sample_value(
            "vectorstep_gateway_tool_denials_total",
            {"mcp_server": "atlassian", "tool": "jira_delete_issue", "agent": "test-agent"},
        ) or 0

        await _run(runner, _agent(), mcp=FakeMCPManager(), tool_policy=_deny_all_policy())

        after = REGISTRY.get_sample_value(
            "vectorstep_gateway_tool_denials_total",
            {"mcp_server": "atlassian", "tool": "jira_delete_issue", "agent": "test-agent"},
        )
        assert after == before + 1

    async def test_no_policy_configured_is_unchanged_passthrough(self):
        """Regression: tool_policy=None (the default) must behave exactly as
        before this feature existed — no evaluation, no tool_denied events."""
        provider = FakeProvider([
            _tool_response("atlassian__jira_delete_issue", "call_1"),
            _text_response("Deleted"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, events = await _run(runner, _agent(), mcp=FakeMCPManager())

        assert result.text == "Deleted"
        assert not [e for e in events if e["type"] == "tool_denied"]

    async def test_default_deny_hides_categorically_blocked_tool_from_schema(self):
        seen_tools = {}

        class RecordingProvider(FakeProvider):
            async def complete(self, system, messages, tools, model, max_tokens, thinking_level=None):
                seen_tools["names"] = [t["name"] for t in (tools or [])]
                return await super().complete(system, messages, tools, model, max_tokens, thinking_level)

        tools = [
            MCPTool(name="jira_search", registered_name="atlassian__jira_search",
                    description="", input_schema={}, server_name="atlassian"),
            MCPTool(name="jira_delete_issue", registered_name="atlassian__jira_delete_issue",
                    description="", input_schema={}, server_name="atlassian"),
        ]
        provider = RecordingProvider([_text_response("no tools needed")])
        runner = AgentRunner(FakeRouter(provider))
        policy = ToolPolicy(ToolPolicyConfig(default="deny", rules=[
            ToolPolicyRule(allow=ToolPolicyMatch(tool="jira_search")),
        ]))
        await _run(runner, _agent(), mcp=FakeMCPManager(tools=tools), tool_policy=policy)

        assert seen_tools["names"] == ["atlassian__jira_search"]


# ---------------------------------------------------------------------------
# Max iterations
# ---------------------------------------------------------------------------


class TestMaxIterations:
    async def test_raises_after_max_iterations(self):
        tool_resp = _tool_response()
        provider = FakeProvider([tool_resp] * 10)
        runner = AgentRunner(FakeRouter(provider))

        with pytest.raises(AgentRunError, match="max iterations"):
            await runner.run(
                agent=_agent(),
                session_key="agent:test-agent:test",
                message="loop",
                model_override=None,
                thinking_level=None,
                mcp_manager=FakeMCPManager(),
                limits=_limits(max_iterations=3),
            )


# ---------------------------------------------------------------------------
# Retry and fallback
# ---------------------------------------------------------------------------


class TestRetry:
    async def test_retry_on_429_then_success(self):
        provider = FakeProvider([
            ProviderError("rate limited", status_code=429),
            _text_response("Recovered"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        # retry_attempts=1 → attempt 0 fails, retries once, attempt 1 succeeds
        result, events = await _run(runner, _agent(), limits=_limits(retry_attempts=1))

        assert result.text == "Recovered"
        retry_events = [e for e in events if e["type"] == "llm_retry"]
        assert len(retry_events) == 1
        assert retry_events[0]["attempt"] == 1

    async def test_retry_on_503(self):
        provider = FakeProvider([
            ProviderError("service unavailable", status_code=503),
            _text_response("OK"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent(), limits=_limits(retry_attempts=1))
        assert result.text == "OK"

    async def test_retry_on_connection_error_no_status(self):
        # status_code=None is treated as retryable (e.g. DNS failure, refused connection)
        provider = FakeProvider([
            ProviderError("DNS resolution failed", status_code=None),
            _text_response("Connected"),
        ])
        runner = AgentRunner(FakeRouter(provider))
        result, _ = await _run(runner, _agent(), limits=_limits(retry_attempts=1))
        assert result.text == "Connected"

    async def test_no_retry_event_on_non_retryable_400(self):
        # 400 is not retryable — falls straight to next candidate, no llm_retry event
        primary = FakeProvider([ProviderError("bad request", status_code=400)])
        fallback = FakeProvider([_text_response("Fallback success")])
        router = FakeRouter(by_model={
            "anthropic/claude-sonnet-4-6": primary,
            "anthropic/claude-haiku-4-5": fallback,
        })
        runner = AgentRunner(router)
        agent = _agent(model="anthropic/claude-sonnet-4-6", fallbacks=["anthropic/claude-haiku-4-5"])
        # retry_attempts=1 but 400 skips retries
        result, events = await _run(runner, agent, limits=_limits(retry_attempts=1))

        assert result.text == "Fallback success"
        assert not any(e["type"] == "llm_retry" for e in events)
        fallback_events = [e for e in events if e["type"] == "model_fallback"]
        assert len(fallback_events) == 1


class TestModelFallback:
    async def test_fallback_after_exhausted_retries(self):
        primary = FakeProvider([ProviderError("overloaded", status_code=529)])
        fallback = FakeProvider([_text_response("Fallback response")])
        router = FakeRouter(by_model={
            "anthropic/claude-sonnet-4-6": primary,
            "anthropic/claude-haiku-4-5": fallback,
        })
        runner = AgentRunner(router)
        agent = _agent(model="anthropic/claude-sonnet-4-6", fallbacks=["anthropic/claude-haiku-4-5"])
        result, events = await _run(runner, agent)

        assert result.text == "Fallback response"
        fallback_events = [e for e in events if e["type"] == "model_fallback"]
        assert len(fallback_events) == 1
        assert fallback_events[0]["from_model"] == "anthropic/claude-sonnet-4-6"
        assert fallback_events[0]["to_model"] == "anthropic/claude-haiku-4-5"

    async def test_all_models_exhausted_raises(self):
        provider = FakeProvider([
            ProviderError("overloaded", status_code=529),
            ProviderError("also overloaded", status_code=529),
        ])
        runner = AgentRunner(FakeRouter(provider))

        with pytest.raises(AgentRunError, match="All models exhausted"):
            await runner.run(
                agent=_agent(model="anthropic/claude-sonnet-4-6", fallbacks=["anthropic/claude-haiku-4-5"]),
                session_key="agent:test-agent:test",
                message="help",
                model_override=None,
                thinking_level=None,
                mcp_manager=FakeMCPManager(),
                limits=_limits(retry_attempts=0),
            )

    async def test_fallback_deduplicates_primary_from_list(self):
        # primary appears in fallbacks → deduplicated to [primary, other]
        # primary fails → falls to other
        primary = FakeProvider([ProviderError("overloaded", status_code=529)])
        other = FakeProvider([_text_response("From other")])
        router = FakeRouter(by_model={
            "anthropic/claude-sonnet-4-6": primary,
            "anthropic/claude-haiku-4-5": other,
        })
        runner = AgentRunner(router)
        agent = _agent(
            model="anthropic/claude-sonnet-4-6",
            fallbacks=["anthropic/claude-sonnet-4-6", "anthropic/claude-haiku-4-5"],
        )
        result, _ = await _run(runner, agent)
        assert result.text == "From other"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_cancelled_error_propagates(self):
        async def slow_complete(*args, **kwargs):
            await asyncio.sleep(10)
            return _text_response()

        class SlowProvider(BaseProvider):
            async def complete(self, *args, **kwargs):
                return await slow_complete()

        runner = AgentRunner(FakeRouter(SlowProvider()))

        async def run_and_cancel():
            task = asyncio.create_task(
                runner.run(
                    agent=_agent(),
                    session_key="agent:test-agent:test",
                    message="slow",
                    model_override=None,
                    thinking_level=None,
                    mcp_manager=FakeMCPManager(),
                    limits=_limits(),
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()
            return await task

        with pytest.raises(asyncio.CancelledError):
            await run_and_cancel()
