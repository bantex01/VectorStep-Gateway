from __future__ import annotations

import json
import logging

import httpx

from gateway.llm.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderResponse,
    raise_if_error_envelope,
)

logger = logging.getLogger(__name__)


class YoloProvider(BaseProvider):
    """Generic OpenAI-compatible chat-completions provider (the "yolo" custom endpoint)."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 180.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

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
                "Yolo does not support extended thinking; ignoring thinking_level=%r",
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
            resp = await self._client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Yolo HTTP {exc.response.status_code}: {exc.response.text}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Yolo request error: {exc}") from exc

        raise_if_error_envelope(data, "Yolo")
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        content_blocks: list[dict] = []

        text_content = message.get("content") or ""
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
