from __future__ import annotations

import json
import logging
import uuid

import httpx

from gateway.llm.providers.base import BaseProvider, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str = "http://localhost:11434/v1", api_key: str = "", timeout: float = 180.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def complete(
        self,
        system: str,
        messages: list,
        tools: list[dict] | None,
        model: str,
        max_tokens: int,
        thinking_level: str | None = None,
    ) -> ProviderResponse:
        if thinking_level and thinking_level != "off":
            logger.warning(
                "Ollama does not support extended thinking; ignoring thinking_level=%r",
                thinking_level,
            )

        all_messages = [{"role": "system", "content": system}, *messages]

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": all_messages,
        }
        if tools:
            payload["tools"] = tools

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Ollama HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Ollama request error: {exc}") from exc

        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        content_blocks: list[dict] = []

        # Some models (e.g. Qwen 3.5) put output in 'reasoning' and leave 'content' empty
        text_content = message.get("content") or ""
        if not text_content and message.get("reasoning"):
            text_content = message["reasoning"]

        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        for tc in message.get("tool_calls") or []:
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": args,
                }
            )

        stop_reason = "tool_use" if finish_reason == "tool_calls" else finish_reason

        usage = data.get("usage", {})
        return ProviderResponse(
            content_blocks=content_blocks,
            stop_reason=stop_reason,
            model_used=data.get("model", model),
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        )


class OllamaCloudProvider(BaseProvider):
    """Provider for Ollama Cloud (https://ollama.com/api/chat).

    Uses Ollama's native /api/chat format, not the OpenAI-compat /v1/ endpoint.
    Tool definitions are sent in OpenAI function format (same as the local Ollama
    native API accepts). Tool call IDs are generated locally when the model omits them.
    """

    def __init__(self, base_url: str = "https://ollama.com/api", api_key: str = "", timeout: float = 180.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @staticmethod
    def _normalize_messages(messages: list) -> list:
        """Convert OpenAI-format message history to native Ollama format.

        The agent runner builds history in OpenAI-compat format (arguments as
        JSON strings, tool_call_id on tool results). Native Ollama /api/chat
        expects arguments as dicts and has no tool_call_id field.
        """
        out = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_calls = []
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    args = func.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            pass
                    tool_calls.append({"function": {"name": func.get("name", ""), "arguments": args}})
                out.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tool_calls})
            elif msg.get("role") == "tool":
                content = msg.get("content", "")
                # mcp_result_to_openrouter json-dumps the MCP content list for non-error
                # results (e.g. '[{"type": "text", "text": "..."}]'). Native Ollama
                # expects a plain string — unwrap it here.
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, list):
                        content = "\n".join(
                            item.get("text", "") for item in parsed
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                except (json.JSONDecodeError, AttributeError):
                    pass
                out.append({"role": "tool", "content": content})
            else:
                out.append(msg)
        return out

    async def complete(
        self,
        system: str,
        messages: list,
        tools: list[dict] | None,
        model: str,
        max_tokens: int,
        thinking_level: str | None = None,
    ) -> ProviderResponse:
        if thinking_level and thinking_level != "off":
            logger.warning(
                "Ollama Cloud does not support extended thinking; ignoring thinking_level=%r",
                thinking_level,
            )

        all_messages = [{"role": "system", "content": system}, *self._normalize_messages(messages)]

        payload: dict = {
            "model": model,
            "messages": all_messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/chat"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Ollama Cloud HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Ollama Cloud request error: {exc}") from exc

        # Native Ollama response: top-level "message" object, not choices[]
        message = data.get("message", {})
        done_reason = data.get("done_reason", "stop")
        logger.debug("Ollama Cloud raw message (done_reason=%s): %s", done_reason, message)

        content_blocks: list[dict] = []

        # Some models put output in 'reasoning' or 'thinking' and leave 'content' empty.
        text_content = message.get("content") or ""
        if not text_content:
            text_content = message.get("reasoning") or message.get("thinking") or ""
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        for tc in message.get("tool_calls") or []:
            func = tc.get("function", {})
            args = func.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            # Native Ollama tool calls may omit IDs — generate one if absent
            tool_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": func.get("name", ""),
                    "input": args,
                }
            )

        # Native Ollama Cloud returns done_reason="stop" even when tool calls are
        # present — derive stop_reason from the message content instead.
        stop_reason = "tool_use" if content_blocks and any(b.get("type") == "tool_use" for b in content_blocks) else done_reason

        return ProviderResponse(
            content_blocks=content_blocks,
            stop_reason=stop_reason,
            model_used=data.get("model", model),
            usage={
                # Native Ollama uses prompt_eval_count / eval_count
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
            },
        )
