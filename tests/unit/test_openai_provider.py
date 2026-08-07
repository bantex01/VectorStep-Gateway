"""Tests for OpenAIProvider."""
import pytest
from unittest.mock import MagicMock, patch

import httpx

from gateway.llm.providers.openai import OpenAIProvider
from gateway.llm.providers.base import ProviderError


def _provider(base_url=None, api_key="test-key"):
    kwargs = {"api_key": api_key}
    if base_url is not None:
        kwargs["base_url"] = base_url
    return OpenAIProvider(**kwargs)


def _ok_response(content="Hello from OpenAI", tool_calls=None, finish_reason=None, model="gpt-5"):
    msg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
    choice = {
        "message": msg,
        "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [choice],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "model": model,
    }
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# complete() — request shape
# ---------------------------------------------------------------------------

async def test_complete_sends_bearer_auth_header():
    p = _provider()
    sent_headers = {}

    async def fake_post(url, json=None, headers=None):
        sent_headers.update(headers or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-5", 1024)

    assert sent_headers.get("Authorization") == "Bearer test-key"


async def test_complete_sends_max_completion_tokens_not_max_tokens():
    p = _provider()
    sent_payload = {}

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-5", 1024)

    assert sent_payload["max_completion_tokens"] == 1024
    assert "max_tokens" not in sent_payload


async def test_complete_includes_model_in_payload():
    p = _provider()
    sent_payload = {}

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-5", 1024)

    assert sent_payload["model"] == "gpt-5"


async def test_default_base_url():
    p = _provider()
    sent_urls = []

    async def fake_post(url, json=None, headers=None):
        sent_urls.append(url)
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-5", 1024)

    assert sent_urls[0] == "https://api.openai.com/v1/chat/completions"


async def test_base_url_override():
    p = _provider(base_url="https://my-proxy.example.com/v1")
    sent_urls = []

    async def fake_post(url, json=None, headers=None):
        sent_urls.append(url)
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-5", 1024)

    assert sent_urls[0] == "https://my-proxy.example.com/v1/chat/completions"


async def test_complete_includes_system_message():
    p = _provider()
    sent_payload = {}

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("You are a helpful assistant.", [], None, "gpt-5", 1024)

    assert sent_payload["messages"][0] == {
        "role": "system",
        "content": "You are a helpful assistant.",
    }


async def test_complete_includes_tools_when_provided():
    p = _provider()
    sent_payload = {}
    tools = [{"type": "function", "function": {"name": "get_weather"}}]

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], tools, "gpt-5", 1024)

    assert sent_payload["tools"] == tools


async def test_complete_omits_tools_when_none():
    p = _provider()
    sent_payload = {}

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-5", 1024)

    assert "tools" not in sent_payload


# ---------------------------------------------------------------------------
# complete() — response parsing
# ---------------------------------------------------------------------------

async def test_complete_returns_text_block():
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        return _ok_response(content="Here is my answer.")

    with patch.object(p._client, "post", new=fake_post):
        result = await p.complete("sys", [], None, "gpt-5", 1024)

    assert len(result.content_blocks) == 1
    assert result.content_blocks[0] == {"type": "text", "text": "Here is my answer."}
    assert result.stop_reason == "stop"
    assert result.model_used == "gpt-5"


async def test_complete_returns_tool_use_block():
    p = _provider()
    tool_calls = [
        {
            "id": "call_abc123",
            "function": {"name": "search", "arguments": '{"query": "openai api"}'},
        }
    ]

    async def fake_post(url, json=None, headers=None):
        return _ok_response(content=None, tool_calls=tool_calls)

    with patch.object(p._client, "post", new=fake_post):
        result = await p.complete("sys", [], None, "gpt-5", 1024)

    assert result.stop_reason == "tool_use"
    tool_block = result.content_blocks[0]
    assert tool_block["type"] == "tool_use"
    assert tool_block["id"] == "call_abc123"
    assert tool_block["name"] == "search"
    assert tool_block["input"] == {"query": "openai api"}


async def test_complete_token_usage():
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        result = await p.complete("sys", [], None, "gpt-5", 1024)

    assert result.usage["input_tokens"] == 10
    assert result.usage["output_tokens"] == 20


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

async def test_http_error_raises_provider_error():
    p = _provider()
    err_resp = MagicMock()
    err_resp.status_code = 401
    err_resp.text = "Unauthorized"

    async def fake_post(url, json=None, headers=None):
        raise httpx.HTTPStatusError("401", request=MagicMock(), response=err_resp)

    with patch.object(p._client, "post", new=fake_post):
        with pytest.raises(ProviderError) as exc_info:
            await p.complete("sys", [], None, "gpt-5", 1024)

    assert exc_info.value.status_code == 401
    assert "OpenAI" in str(exc_info.value)


async def test_request_error_raises_provider_error():
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        raise httpx.ConnectError("connection refused")

    with patch.object(p._client, "post", new=fake_post):
        with pytest.raises(ProviderError) as exc_info:
            await p.complete("sys", [], None, "gpt-5", 1024)

    assert "request error" in str(exc_info.value).lower()


async def test_error_envelope_with_200_status_raises_provider_error():
    """Some OpenAI-compat proxies return HTTP 200 with an {"error": ...} body."""
    p = _provider()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"error": {"message": "rate limited", "code": 429}}
    resp.raise_for_status = MagicMock()

    async def fake_post(url, json=None, headers=None):
        return resp

    with patch.object(p._client, "post", new=fake_post):
        with pytest.raises(ProviderError) as exc_info:
            await p.complete("sys", [], None, "gpt-5", 1024)

    assert exc_info.value.status_code == 429


async def test_thinking_level_warning_logged(caplog):
    import logging
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        with caplog.at_level(logging.WARNING, logger="gateway.llm.providers.openai"):
            await p.complete("sys", [], None, "gpt-5", 1024, thinking_level="high")

    assert "thinking_level" in caplog.text
