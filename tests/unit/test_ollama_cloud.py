"""Tests for OllamaCloudProvider: _normalize_messages and stop_reason derivation."""
import json

import pytest

from gateway.llm.providers.ollama import OllamaCloudProvider


class TestNormalizeMessages:
    def _norm(self, messages):
        return OllamaCloudProvider._normalize_messages(messages)

    def test_plain_user_message_passthrough(self):
        messages = [{"role": "user", "content": "hello"}]
        result = self._norm(messages)
        assert result == messages

    def test_plain_assistant_no_tool_calls_passthrough(self):
        messages = [{"role": "assistant", "content": "I can help"}]
        result = self._norm(messages)
        assert result == messages

    def test_assistant_with_string_arguments_parsed(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "search", "arguments": '{"q": "test"}'}}
                ],
            }
        ]
        result = self._norm(messages)
        assert result[0]["role"] == "assistant"
        assert result[0]["tool_calls"][0]["function"]["arguments"] == {"q": "test"}

    def test_assistant_with_dict_arguments_passthrough(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "search", "arguments": {"q": "test"}}}
                ],
            }
        ]
        result = self._norm(messages)
        assert result[0]["tool_calls"][0]["function"]["arguments"] == {"q": "test"}

    def test_assistant_multiple_tool_calls(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
                    {"function": {"name": "tool_b", "arguments": '{"y": 2}'}},
                ],
            }
        ]
        result = self._norm(messages)
        assert result[0]["tool_calls"][0]["function"]["arguments"] == {"x": 1}
        assert result[0]["tool_calls"][1]["function"]["arguments"] == {"y": 2}

    def test_tool_role_json_list_unwrapped_to_text(self):
        content = json.dumps([{"type": "text", "text": "result text"}])
        messages = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
        result = self._norm(messages)
        assert result[0]["role"] == "tool"
        assert result[0]["content"] == "result text"
        assert "tool_call_id" not in result[0]

    def test_tool_role_multiple_text_blocks(self):
        content = json.dumps([
            {"type": "text", "text": "line 1"},
            {"type": "text", "text": "line 2"},
        ])
        messages = [{"role": "tool", "content": content}]
        result = self._norm(messages)
        assert result[0]["content"] == "line 1\nline 2"

    def test_tool_role_plain_string_unchanged(self):
        messages = [{"role": "tool", "content": "plain result"}]
        result = self._norm(messages)
        assert result[0]["content"] == "plain result"

    def test_tool_role_error_string_unchanged(self):
        messages = [{"role": "tool", "content": "Connection refused"}]
        result = self._norm(messages)
        assert result[0]["content"] == "Connection refused"

    def test_mixed_conversation(self):
        content_json = json.dumps([{"type": "text", "text": "grafana_result"}])
        messages = [
            {"role": "user", "content": "check grafana"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "grafana__query", "arguments": '{"expr": "up"}'}}],
            },
            {"role": "tool", "content": content_json},
        ]
        result = self._norm(messages)
        assert result[0] == {"role": "user", "content": "check grafana"}
        assert result[1]["tool_calls"][0]["function"]["arguments"] == {"expr": "up"}
        assert result[2]["content"] == "grafana_result"

    def test_empty_messages_returns_empty(self):
        assert self._norm([]) == []

    def test_tool_role_non_list_json_unchanged(self):
        # JSON but not a list → keep as-is
        content = json.dumps({"type": "text", "text": "hmm"})
        messages = [{"role": "tool", "content": content}]
        result = self._norm(messages)
        assert result[0]["content"] == content
