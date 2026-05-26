from __future__ import annotations

import asyncio
import json
import logging
import time

from pydantic import BaseModel

from gateway.llm.router import LLMRouter
from gateway.llm.tool_translator import (
    mcp_result_to_anthropic,
    mcp_result_to_openrouter,
    mcp_to_anthropic,
    mcp_to_openrouter,
)
from gateway.mcp.manager import MCPManager, MCPToolResult
from gateway.models.agent import AgentConfig
from gateway.models.config import LimitsConfig

logger = logging.getLogger(__name__)


class AgentRunResult(BaseModel):
    text: str
    model_used: str
    duration_ms: int
    tool_calls_made: int
    iterations: int
    usage: dict  # {"input_tokens": N, "output_tokens": N} accumulated across iterations


class AgentRunError(Exception):
    pass


class AgentRunner:
    def __init__(self, router: LLMRouter) -> None:
        self._router = router

    async def run(
        self,
        agent: AgentConfig,
        session_key: str,
        message: str,
        model_override: str | None,
        thinking_level: str | None,
        mcp_manager: MCPManager,
        limits: LimitsConfig,
    ) -> AgentRunResult:
        start = time.monotonic()

        model_string = model_override or agent.model
        provider, model_name = self._router.get_provider_and_model(model_string)
        is_openrouter = model_string.startswith("openrouter/")

        # Resolve tools for this agent and translate to provider format
        mcp_tools = mcp_manager.get_tools_for_agent(agent)
        if mcp_tools:
            translated_tools = (
                mcp_to_openrouter(mcp_tools) if is_openrouter else mcp_to_anthropic(mcp_tools)
            )
        else:
            translated_tools = None

        messages: list[dict] = [{"role": "user", "content": message}]
        iterations = 0
        tool_calls_made = 0
        model_used = model_name
        total_input_tokens = 0
        total_output_tokens = 0

        while True:
            # ── LLM call ──────────────────────────────────────────────
            try:
                response = await asyncio.wait_for(
                    provider.complete(
                        system=agent.soul,
                        messages=messages,
                        tools=translated_tools,
                        model=model_name,
                        max_tokens=agent.max_tokens,
                        thinking_level=thinking_level,
                    ),
                    timeout=float(limits.request_timeout_seconds),
                )
            except asyncio.TimeoutError:
                raise AgentRunError(
                    f"LLM request timed out after {limits.request_timeout_seconds}s"
                )

            model_used = response.model_used
            total_input_tokens += response.usage.get("input_tokens", 0)
            total_output_tokens += response.usage.get("output_tokens", 0)

            tool_use_blocks = [
                b for b in response.content_blocks if b.get("type") == "tool_use"
            ]

            # ── No tool calls → final response ────────────────────────
            if not tool_use_blocks or response.stop_reason not in ("tool_use", "tool_calls"):
                text = "\n".join(
                    b["text"]
                    for b in response.content_blocks
                    if b.get("type") == "text" and b.get("text")
                )
                return AgentRunResult(
                    text=text,
                    model_used=model_used,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    tool_calls_made=tool_calls_made,
                    iterations=iterations,
                    usage={
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                )

            # ── Append assistant message ───────────────────────────────
            if is_openrouter:
                text_content = "\n".join(
                    b["text"]
                    for b in response.content_blocks
                    if b.get("type") == "text" and b.get("text")
                )
                tool_calls_field = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {
                            "name": b["name"],
                            "arguments": (
                                b["input"]
                                if isinstance(b["input"], str)
                                else json.dumps(b["input"])
                            ),
                        },
                    }
                    for b in tool_use_blocks
                ]
                assistant_msg: dict = {
                    "role": "assistant",
                    "content": text_content,
                    "tool_calls": tool_calls_field,
                }
            else:
                # Anthropic expects the raw content blocks (including thinking blocks)
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content_blocks,
                }

            messages.append(assistant_msg)

            # ── Execute tool calls ────────────────────────────────────
            tool_results: list[dict] = []
            for block in tool_use_blocks:
                tool_calls_made += 1
                tool_use_id = block["id"]
                tool_name = block["name"]
                arguments: dict = block["input"]

                try:
                    result = await asyncio.wait_for(
                        mcp_manager.call_tool(tool_name, arguments),
                        timeout=float(limits.mcp_tool_timeout_seconds),
                    )
                    logger.info("Tool '%s' succeeded", tool_name)
                except asyncio.TimeoutError:
                    result = MCPToolResult(
                        content=[
                            {
                                "type": "text",
                                "text": (
                                    f"Tool '{tool_name}' timed out after "
                                    f"{limits.mcp_tool_timeout_seconds}s"
                                ),
                            }
                        ],
                        is_error=True,
                    )
                    logger.warning("Tool '%s' timed out", tool_name)
                except Exception as exc:
                    result = MCPToolResult(
                        content=[{"type": "text", "text": f"Tool '{tool_name}' error: {exc}"}],
                        is_error=True,
                    )
                    logger.warning("Tool '%s' error: %s", tool_name, exc)

                if is_openrouter:
                    tool_results.append(mcp_result_to_openrouter(tool_use_id, result))
                else:
                    tool_results.append(mcp_result_to_anthropic(tool_use_id, result))

            # Append tool results (Anthropic: batched in one user message;
            # OpenRouter: individual tool messages)
            if is_openrouter:
                messages.extend(tool_results)
            else:
                messages.append({"role": "user", "content": tool_results})

            # ── Iteration guard ───────────────────────────────────────
            iterations += 1
            if iterations >= limits.max_agent_iterations:
                raise AgentRunError(
                    f"max iterations exceeded: {limits.max_agent_iterations}"
                )
