from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class ProviderResponse(BaseModel):
    content_blocks: list[dict]   # normalised content blocks from the LLM
    stop_reason: str             # "end_turn", "tool_use", "max_tokens", etc.
    model_used: str              # actual model name returned by the API
    usage: dict                  # {"input_tokens": N, "output_tokens": N}


class ProviderError(Exception):
    pass


class BaseProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        system: str,
        messages: list,
        tools: list[dict] | None,
        model: str,
        max_tokens: int,
        thinking_level: str | None = None,
    ) -> ProviderResponse: ...
