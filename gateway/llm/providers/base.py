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


def raise_if_error_envelope(data: dict, provider_label: str) -> None:
    """Some providers respond HTTP 200 with an {"error": ...} body instead of a
    non-2xx status — e.g. OpenRouter, when the upstream model provider it's
    proxying to fails (common on free-tier models). httpx's raise_for_status()
    never sees this since the status is 200, so without this check, code that
    blindly indexes into the success shape (data["choices"][0], etc.) crashes
    with an unhelpful KeyError instead of a classified, retryable ProviderError.
    """
    error = data.get("error")
    if not error:
        return
    if isinstance(error, dict):
        message = error.get("message") or str(error)
        code = error.get("code")
        status_code = code if isinstance(code, int) else None
    else:
        message = str(error)
        status_code = None
    raise ProviderError(f"{provider_label} error: {message}", status_code=status_code)


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

    async def aclose(self) -> None:
        """Release any held connections (HTTP client, etc.). No-op by default."""
