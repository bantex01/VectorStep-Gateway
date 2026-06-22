from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class ProviderResponse(BaseModel):
    content_blocks: list[dict]   # normalised content blocks from the LLM
    stop_reason: str             # "end_turn", "tool_use", "max_tokens", etc.
    model_used: str              # actual model name returned by the API
    usage: dict                  # {"input_tokens": N, "output_tokens": N}


class ProviderError(Exception):
    """Raised when a provider API call fails.

    status_code is the HTTP status code if known (None for connection-level
    failures like DNS/refused-connection errors) — used by the agent runner to
    decide whether an error is worth retrying/falling back on.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
