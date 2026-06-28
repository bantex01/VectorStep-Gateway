"""Tests for AzureOpenAIProvider."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gateway.llm.providers.azure import AzureOpenAIProvider
from gateway.llm.providers.base import ProviderError


def _provider(resource_name="my-org", api_version="2025-01-01-preview"):
    return AzureOpenAIProvider(
        api_key="test-key",
        resource_name=resource_name,
        api_version=api_version,
    )


def _ok_response(content="Hello from Azure", tool_calls=None, finish_reason=None):
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
        "model": "gpt-4o",
    }
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Endpoint construction
# ---------------------------------------------------------------------------

def test_endpoint_format():
    p = _provider(resource_name="contoso-ai", api_version="2024-12-01-preview")
    url = p._endpoint("gpt-4o")
    assert url == (
        "https://contoso-ai.openai.azure.com"
        "/openai/deployments/gpt-4o"
        "/chat/completions?api-version=2024-12-01-preview"
    )


def test_endpoint_uses_deployment_as_model():
    p = _provider()
    assert "/deployments/gpt-4o-mini/" in p._endpoint("gpt-4o-mini")


# ---------------------------------------------------------------------------
# complete() — request shape
# ---------------------------------------------------------------------------

async def test_complete_sends_api_key_header():
    p = _provider()
    sent_headers = {}

    async def fake_post(url, json=None, headers=None):
        sent_headers.update(headers or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-4o", 1024)

    assert sent_headers.get("api-key") == "test-key"
    assert "Authorization" not in sent_headers


async def test_complete_does_not_include_model_in_payload():
    """Azure deployment is encoded in the URL; the model field in body is omitted."""
    p = _provider()
    sent_payload = {}

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-4o", 1024)

    assert "model" not in sent_payload


async def test_complete_sends_correct_url():
    p = _provider(resource_name="my-resource", api_version="2025-01-01-preview")
    sent_urls = []

    async def fake_post(url, json=None, headers=None):
        sent_urls.append(url)
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-4o", 512)

    assert len(sent_urls) == 1
    assert "my-resource.openai.azure.com" in sent_urls[0]
    assert "/deployments/gpt-4o/" in sent_urls[0]
    assert "api-version=2025-01-01-preview" in sent_urls[0]


async def test_complete_includes_system_message():
    p = _provider()
    sent_payload = {}

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("You are a helpful assistant.", [], None, "gpt-4o", 1024)

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
        await p.complete("sys", [], tools, "gpt-4o", 1024)

    assert sent_payload["tools"] == tools


async def test_complete_omits_tools_when_none():
    p = _provider()
    sent_payload = {}

    async def fake_post(url, json=None, headers=None):
        sent_payload.update(json or {})
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        await p.complete("sys", [], None, "gpt-4o", 1024)

    assert "tools" not in sent_payload


# ---------------------------------------------------------------------------
# complete() — response parsing
# ---------------------------------------------------------------------------

async def test_complete_returns_text_block():
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        return _ok_response(content="Here is my answer.")

    with patch.object(p._client, "post", new=fake_post):
        result = await p.complete("sys", [], None, "gpt-4o", 1024)

    assert len(result.content_blocks) == 1
    assert result.content_blocks[0] == {"type": "text", "text": "Here is my answer."}
    assert result.stop_reason == "stop"
    assert result.model_used == "azure/gpt-4o"


async def test_complete_returns_tool_use_block():
    p = _provider()
    tool_calls = [
        {
            "id": "call_abc123",
            "function": {"name": "search", "arguments": '{"query": "azure openai"}'},
        }
    ]

    async def fake_post(url, json=None, headers=None):
        return _ok_response(content=None, tool_calls=tool_calls)

    with patch.object(p._client, "post", new=fake_post):
        result = await p.complete("sys", [], None, "gpt-4o", 1024)

    assert result.stop_reason == "tool_use"
    tool_block = result.content_blocks[0]
    assert tool_block["type"] == "tool_use"
    assert tool_block["id"] == "call_abc123"
    assert tool_block["name"] == "search"
    assert tool_block["input"] == {"query": "azure openai"}


async def test_complete_model_used_prefixed_with_azure():
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        result = await p.complete("sys", [], None, "gpt-4o-mini", 512)

    assert result.model_used == "azure/gpt-4o-mini"


async def test_complete_token_usage():
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        result = await p.complete("sys", [], None, "gpt-4o", 1024)

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
            await p.complete("sys", [], None, "gpt-4o", 1024)

    assert exc_info.value.status_code == 401
    assert "Azure OpenAI" in str(exc_info.value)


async def test_request_error_raises_provider_error():
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        raise httpx.ConnectError("connection refused")

    with patch.object(p._client, "post", new=fake_post):
        with pytest.raises(ProviderError) as exc_info:
            await p.complete("sys", [], None, "gpt-4o", 1024)

    assert "request error" in str(exc_info.value).lower()


async def test_thinking_level_warning_logged(caplog):
    import logging
    p = _provider()

    async def fake_post(url, json=None, headers=None):
        return _ok_response()

    with patch.object(p._client, "post", new=fake_post):
        with caplog.at_level(logging.WARNING, logger="gateway.llm.providers.azure"):
            await p.complete("sys", [], None, "gpt-4o", 1024, thinking_level="high")

    assert "thinking_level" in caplog.text
