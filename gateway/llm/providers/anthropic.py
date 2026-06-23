from __future__ import annotations

import logging

from anthropic import APIError, AsyncAnthropic

from gateway.llm.providers.base import BaseProvider, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)

# thinking_level → budget_tokens; absent/off means no thinking param
_THINKING_BUDGET: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
}


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(
        self,
        system: str,
        messages: list,
        tools: list[dict] | None,
        model: str,
        max_tokens: int,
        thinking_level: str | None = None,
    ) -> ProviderResponse:
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            # Cache the (typically large, unchanging) soul prompt so it's only
            # re-billed as input tokens when its content actually changes.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
        }

        if tools:
            # Marking the last tool definition as a cache breakpoint caches the
            # entire tools array prefix, since Anthropic caching is prefix-based.
            tools = [dict(t) for t in tools]
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tools

        budget = _THINKING_BUDGET.get(thinking_level or "off")
        if budget:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            kwargs["max_tokens"] = max_tokens + budget

        try:
            response = await self._client.messages.create(**kwargs)
        except APIError as exc:
            raise ProviderError(
                f"Anthropic API error: {exc}",
                status_code=getattr(exc, "status_code", None),
            ) from exc

        content_blocks = [block.model_dump() for block in response.content]

        return ProviderResponse(
            content_blocks=content_blocks,
            stop_reason=response.stop_reason or "end_turn",
            model_used=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )
