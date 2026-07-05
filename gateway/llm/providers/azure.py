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


class AzureOpenAIProvider(BaseProvider):
    """Azure OpenAI provider.

    Azure's chat completions API is OpenAI-compatible. The key differences from
    OpenRouter are:
      - The endpoint encodes both the resource name and deployment name in the URL.
      - Auth uses the `api-key` request header rather than `Authorization: Bearer`.
      - The `api-version` query parameter is required.

    Model string format: ``azure/<deployment-name>``
    The deployment name is whatever you named the model when you set it up in the
    Azure AI Foundry / Azure OpenAI Studio (e.g. ``gpt-4o``, ``gpt-4o-mini``).

    Required config.yaml keys (under ``providers.azure``):
        api_key        — Azure OpenAI API key
        resource_name  — Azure resource name (subdomain of .openai.azure.com)
        api_version    — API version (default: 2025-01-01-preview)
    """

    def __init__(
        self,
        api_key: str,
        resource_name: str,
        api_version: str = "2025-01-01-preview",
        timeout: float = 180.0,
    ) -> None:
        self._api_key = api_key
        self._resource_name = resource_name
        self._api_version = api_version
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _endpoint(self, deployment: str) -> str:
        return (
            f"https://{self._resource_name}.openai.azure.com"
            f"/openai/deployments/{deployment}"
            f"/chat/completions?api-version={self._api_version}"
        )

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
                "Azure OpenAI does not support extended thinking; ignoring thinking_level=%r",
                thinking_level,
            )

        all_messages = [{"role": "system", "content": system}, *messages]

        payload: dict = {
            # max_completion_tokens works across both classic (gpt-4o) and reasoning
            # (gpt-5/o1/o3) deployments; the older max_tokens param is rejected outright
            # by reasoning-family models.
            "max_completion_tokens": max_tokens,
            "messages": all_messages,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "api-key": self._api_key,
            "Content-Type": "application/json",
        }

        url = self._endpoint(model)
        logger.debug("AzureOpenAI: POST %s", url)

        try:
            resp = await self._client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Azure OpenAI HTTP {exc.response.status_code}: {exc.response.text}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Azure OpenAI request error: {exc}") from exc

        raise_if_error_envelope(data, "Azure OpenAI")
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
            model_used=f"azure/{model}",
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        )
