"""Tests for MCP ↔ LLM provider tool format translation."""
import json

import pytest

from gateway.llm.tool_translator import (
    anthropic_tool_use_to_mcp,
    mcp_result_to_anthropic,
    mcp_result_to_openrouter,
    mcp_to_anthropic,
    mcp_to_openrouter,
    mcp_to_google,
    mcp_to_ollama,
    mcp_to_ollama_cloud,
    openrouter_tool_call_to_mcp,
)
from gateway.mcp.manager import MCPTool, MCPToolResult


def _make_tool(name="grafana__query", original="query", description="Run a query", server="grafana"):
    return MCPTool(
        name=original,
        registered_name=name,
        description=description,
        input_schema={"type": "object", "properties": {"expr": {"type": "string"}}},
        server_name=server,
    )


class TestMcpToAnthropic:
    def test_single_tool_shape(self):
        tools = [_make_tool()]
        result = mcp_to_anthropic(tools)
        assert len(result) == 1
        assert result[0] == {
            "name": "grafana__query",
            "description": "Run a query",
            "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}},
        }

    def test_multiple_tools(self):
        tools = [_make_tool("grafana__query"), _make_tool("atlassian__search", "search", "Search Jira")]
        result = mcp_to_anthropic(tools)
        assert len(result) == 2
        assert result[0]["name"] == "grafana__query"
        assert result[1]["name"] == "atlassian__search"

    def test_empty_list(self):
        assert mcp_to_anthropic([]) == []


class TestMcpToOpenrouter:
    def test_single_tool_shape(self):
        tools = [_make_tool()]
        result = mcp_to_openrouter(tools)
        assert len(result) == 1
        assert result[0] == {
            "type": "function",
            "function": {
                "name": "grafana__query",
                "description": "Run a query",
                "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}},
            },
        }

    def test_multiple_tools(self):
        tools = [_make_tool("t1", description="First"), _make_tool("t2", description="Second")]
        result = mcp_to_openrouter(tools)
        assert len(result) == 2
        assert result[0]["function"]["name"] == "t1"
        assert result[1]["function"]["name"] == "t2"

    def test_empty_list(self):
        assert mcp_to_openrouter([]) == []


class TestAliases:
    def test_mcp_to_ollama_is_same_as_openrouter(self):
        tools = [_make_tool()]
        assert mcp_to_ollama(tools) == mcp_to_openrouter(tools)

    def test_mcp_to_ollama_cloud_is_same_as_openrouter(self):
        tools = [_make_tool()]
        assert mcp_to_ollama_cloud(tools) == mcp_to_openrouter(tools)

    def test_mcp_to_google_is_same_as_openrouter(self):
        tools = [_make_tool()]
        assert mcp_to_google(tools) == mcp_to_openrouter(tools)


class TestMcpResultToAnthropic:
    def test_success_result(self):
        result = MCPToolResult(
            content=[{"type": "text", "text": "Dashboard found"}],
            is_error=False,
        )
        msg = mcp_result_to_anthropic("tool_use_123", result)
        assert msg["type"] == "tool_result"
        assert msg["tool_use_id"] == "tool_use_123"
        assert msg["is_error"] is False
        assert msg["content"] == [{"type": "text", "text": "Dashboard found"}]

    def test_error_result_wraps_text(self):
        result = MCPToolResult(
            content=[{"type": "text", "text": "Connection refused"}],
            is_error=True,
        )
        msg = mcp_result_to_anthropic("tool_use_456", result)
        assert msg["is_error"] is True
        assert msg["content"] == [{"type": "text", "text": "Connection refused"}]

    def test_error_result_extracts_text_from_content(self):
        result = MCPToolResult(
            content=[
                {"type": "text", "text": "Line 1"},
                {"type": "text", "text": "Line 2"},
            ],
            is_error=True,
        )
        msg = mcp_result_to_anthropic("id", result)
        assert msg["content"] == [{"type": "text", "text": "Line 1\nLine 2"}]


class TestMcpResultToOpenrouter:
    def test_success_result_json_dumps_content(self):
        content = [{"type": "text", "text": "Query result"}]
        result = MCPToolResult(content=content, is_error=False)
        msg = mcp_result_to_openrouter("call_789", result)
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_789"
        # Content is JSON-encoded for non-error results
        assert json.loads(msg["content"]) == content

    def test_error_result_plain_text(self):
        result = MCPToolResult(
            content=[{"type": "text", "text": "Error occurred"}],
            is_error=True,
        )
        msg = mcp_result_to_openrouter("call_err", result)
        assert msg["role"] == "tool"
        assert msg["content"] == "Error occurred"


class TestToolCallExtractors:
    def test_anthropic_tool_use_to_mcp(self):
        block = {"type": "tool_use", "id": "abc", "name": "grafana__query", "input": {"expr": "up"}}
        name, args = anthropic_tool_use_to_mcp(block)
        assert name == "grafana__query"
        assert args == {"expr": "up"}

    def test_openrouter_tool_call_to_mcp(self):
        block = {"type": "tool_use", "id": "abc", "name": "atlassian__search", "input": {"query": "bug"}}
        name, args = openrouter_tool_call_to_mcp(block)
        assert name == "atlassian__search"
        assert args == {"query": "bug"}
