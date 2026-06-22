from __future__ import annotations

import json
import logging

import httpx

from gateway.llm.providers.base import BaseProvider, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(BaseProvider):
    def __init__(self, api_key: str, timeout: float = 180.0) -> None:
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
                "OpenRouter does not support extended thinking; ignoring thinking_level=%r",
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

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(_OPENROUTER_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"OpenRouter HTTP {exc.response.status_code}: {exc.response.text}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"OpenRouter request error: {exc}") from exc

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
