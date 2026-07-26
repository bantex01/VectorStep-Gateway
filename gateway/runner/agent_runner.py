from __future__ import annotations

import asyncio
import json
import logging
import time

from opentelemetry import context as otel_context
from opentelemetry import trace
from pydantic import BaseModel

from gateway.llm.providers.base import ProviderError
from gateway.llm.router import LLMRouter, provider_key_for_model_string
from gateway.llm.tool_translator import (
    mcp_result_to_anthropic,
    mcp_result_to_openrouter,
    mcp_to_anthropic,
    mcp_to_openrouter,
)
from gateway.mcp.manager import MCPManager, MCPToolResult
from gateway.metrics import record_agent_run_complete, record_agent_run_start, record_tool_call
from gateway.models.agent import AgentConfig
from gateway.models.config import LimitsConfig
from gateway.tracing import tracer

logger = logging.getLogger(__name__)


# HTTP statuses worth retrying/falling back on. None (connection-level errors with
# no status code, e.g. DNS/refused-connection/Anthropic APIConnectionError) is
# treated as retryable too — see _call_llm.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class AgentRunResult(BaseModel):
    text: str
    model_used: str
    provider: str
    duration_ms: int
    tool_calls_made: int
    iterations: int
    usage: dict  # {"input_tokens": N, "output_tokens": N} accumulated across iterations
    trace: list[dict] = []  # ordered agent execution trace — always captured
    agent_version: str = ""


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
        on_trace_event=None,  # Optional async callable(event: dict) — called after each trace event
        remote_context: otel_context.Context | None = None,
        trace_tool_result_max: int | None = None,  # per-request override of limits.trace_tool_result_max_chars
    ) -> AgentRunResult:
        start = time.monotonic()
        record_agent_run_start()
        trace_tool_result_max = trace_tool_result_max or limits.trace_tool_result_max_chars

        model_string = model_override or agent.model
        # Models to try in order for this run: the requested one, then
        # agent.yaml's model_fallbacks. Once a candidate succeeds, later
        # iterations in this same run try it first (no flapping back to a
        # model that already failed).
        candidate_model_strings = _dedupe_preserve_order([model_string, *agent.model_fallbacks])
        candidate_index = 0

        # Resolve tools for this agent once, pre-translated to both provider
        # formats — which one is used depends on which candidate model ends
        # up serving the request.
        mcp_tools = mcp_manager.get_tools_for_agent(agent)
        anthropic_tools = mcp_to_anthropic(mcp_tools) if mcp_tools else None
        openai_tools = mcp_to_openrouter(mcp_tools) if mcp_tools else None

        messages: list[dict] = [{"role": "user", "content": message}]
        iterations = 0
        tool_calls_made = 0
        model_used = model_string
        is_openai_compat = False
        total_input_tokens = 0
        total_output_tokens = 0
        trace: list[dict] = []

        async def _emit(event: dict) -> None:
            """Append a trace event and stream it to the caller if a callback is set."""
            trace.append(event)
            if on_trace_event is not None:
                await on_trace_event(event)

        async def _call_llm():
            """Call the LLM for the current iteration.

            Retries transient errors (429/5xx/529/timeouts/connection errors) on
            the same model with exponential backoff; once a candidate's retries
            are exhausted, falls over to the next entry in candidate_model_strings.
            Mutates model_string/model_used/is_openai_compat and candidate_index
            (via nonlocal) to reflect whichever candidate actually succeeds.
            """
            nonlocal candidate_index, model_string, model_used, is_openai_compat
            last_exc: Exception | None = None

            while candidate_index < len(candidate_model_strings):
                candidate_string = candidate_model_strings[candidate_index]
                candidate_provider, candidate_model_name = self._router.get_provider_and_model(
                    candidate_string
                )
                # Every provider except Anthropic expects tool defs in OpenAI's
                # function-calling shape (even ollama-cloud, whose completions
                # endpoint is native rather than OpenAI-compat — see its provider
                # docstring). Deriving this from the canonical provider map means
                # a newly added provider (e.g. "yolo") is classified correctly
                # automatically, instead of needing this list updated by hand —
                # forgetting to is exactly what sent Anthropic-shaped tool defs
                # to an OpenAI-compat endpoint and got "tools[0].function: Field
                # required" back from the upstream API.
                candidate_is_openai_compat = provider_key_for_model_string(candidate_string) != "anthropic"
                candidate_tools = openai_tools if candidate_is_openai_compat else anthropic_tools

                for attempt in range(limits.llm_retry_attempts + 1):
                    with tracer.start_as_current_span(
                        "llm_call",
                        attributes={
                            "llm_call.iteration": iterations,
                            "llm_call.attempt": attempt + 1,
                            "gen_ai.request.model": candidate_model_name,
                        },
                    ) as llm_span:
                        try:
                            response = await asyncio.wait_for(
                                candidate_provider.complete(
                                    system=agent.soul,
                                    messages=messages,
                                    tools=candidate_tools,
                                    model=candidate_model_name,
                                    max_tokens=agent.max_tokens,
                                    thinking_level=thinking_level,
                                ),
                                timeout=float(limits.request_timeout_seconds),
                            )
                        except asyncio.TimeoutError:
                            last_exc = AgentRunError(
                                f"LLM request to {candidate_string} timed out after "
                                f"{limits.request_timeout_seconds}s"
                            )
                            retryable = True
                        except ProviderError as exc:
                            last_exc = exc
                            retryable = (
                                exc.status_code is None
                                or exc.status_code in _RETRYABLE_STATUS_CODES
                            )
                        else:
                            model_string = candidate_string
                            model_used = response.model_used
                            is_openai_compat = candidate_is_openai_compat
                            llm_span.set_attribute("gen_ai.response.model", model_used)
                            llm_span.set_attribute(
                                "gen_ai.usage.input_tokens", response.usage.get("input_tokens", 0)
                            )
                            llm_span.set_attribute(
                                "gen_ai.usage.output_tokens",
                                response.usage.get("output_tokens", 0),
                            )
                            return response

                        llm_span.set_attribute("llm_call.error", str(last_exc))
                        llm_span.set_attribute("llm_call.retryable", retryable)

                    if retryable and attempt < limits.llm_retry_attempts:
                        delay = limits.llm_retry_base_delay_seconds * (2 ** attempt)
                        await _emit({
                            "type": "llm_retry",
                            "model": candidate_string,
                            "attempt": attempt + 1,
                            "delay_seconds": delay,
                            "error": str(last_exc),
                        })
                        logger.warning(
                            "Agent %s — retryable error on %s (attempt %d/%d): %s — retrying in %.1fs",
                            agent.name, candidate_string, attempt + 1,
                            limits.llm_retry_attempts + 1, last_exc, delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        break  # give up on this candidate — fall over below

                candidate_index += 1
                if candidate_index < len(candidate_model_strings):
                    next_model = candidate_model_strings[candidate_index]
                    await _emit({
                        "type": "model_fallback",
                        "from_model": candidate_string,
                        "to_model": next_model,
                        "error": str(last_exc),
                    })
                    logger.warning(
                        "Agent %s — falling back from %s to %s: %s",
                        agent.name, candidate_string, next_model, last_exc,
                    )

            raise AgentRunError(
                f"All models exhausted ({', '.join(candidate_model_strings)}): {last_exc}"
            )

        parent_ctx = remote_context if remote_context is not None else otel_context.Context()

        run_status = "ok"
        try:
            with tracer.start_as_current_span(
                "agent.run",
                context=parent_ctx,
                attributes={
                    "agent.name": agent.name,
                    "pork.session_key": session_key,
                    "gen_ai.request.model": model_string,
                },
            ) as agent_span:
                while True:
                    # ── LLM call ──────────────────────────────────────────────
                    iterations += 1
                    await _emit({"type": "llm_call", "iteration": iterations})
                    logger.debug("Agent %s — LLM call #%d", agent.name, iterations)

                    response = await _call_llm()
                    total_input_tokens += response.usage.get("input_tokens", 0)
                    total_output_tokens += response.usage.get("output_tokens", 0)

                    # ── Capture thinking and text blocks into trace ───────────
                    for block in response.content_blocks:
                        btype = block.get("type")
                        if btype == "thinking":
                            content = block.get("thinking", "")
                            await _emit({"type": "thinking", "content": content})
                            logger.debug("Agent %s — thinking (%d chars)", agent.name, len(content))
                        elif btype == "text" and block.get("text"):
                            content = block["text"]
                            await _emit({"type": "text", "content": content})
                            logger.debug("Agent %s — text output (%d chars)", agent.name, len(content))

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
                        agent_span.set_attribute("gen_ai.response.model", model_used)
                        agent_span.set_attribute("gen_ai.usage.input_tokens", total_input_tokens)
                        agent_span.set_attribute("gen_ai.usage.output_tokens", total_output_tokens)
                        agent_span.set_attribute("pork.gateway.iterations", iterations)
                        agent_span.set_attribute("pork.gateway.tool_calls", tool_calls_made)
                        run_status = "ok"
                        return AgentRunResult(
                            text=text,
                            model_used=model_used,
                            # model_string is the candidate that actually succeeded (post
                            # cross-provider fallback) — not the originally requested model.
                            provider=provider_key_for_model_string(model_string),
                            duration_ms=int((time.monotonic() - start) * 1000),
                            tool_calls_made=tool_calls_made,
                            iterations=iterations,
                            usage={
                                "input_tokens": total_input_tokens,
                                "output_tokens": total_output_tokens,
                            },
                            trace=trace,
                            agent_version=agent.version,
                        )

                    # ── Append assistant message ───────────────────────────────
                    if is_openai_compat:
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

                    # ── Execute tool calls (concurrently — independent tool_use
                    # blocks in one turn don't need to wait on each other) ─────
                    async def _execute_tool_call(block: dict) -> dict:
                        tool_use_id = block["id"]
                        tool_name = block["name"]
                        arguments: dict = block["input"]

                        await _emit({"type": "tool_call", "name": tool_name, "input": arguments})
                        logger.debug("Agent %s — tool call: %s %s", agent.name, tool_name, arguments)

                        tool_start = time.monotonic()
                        tool_result_str = "success"
                        with tracer.start_as_current_span(
                            f"tool_call {tool_name}",
                            attributes={"tool.name": tool_name},
                        ) as tool_span:
                            try:
                                result = await asyncio.wait_for(
                                    mcp_manager.call_tool(tool_name, arguments),
                                    timeout=float(limits.mcp_tool_timeout_seconds),
                                )
                                logger.info("Tool '%s' succeeded", tool_name)
                                tool_span.set_attribute("tool.is_error", False)
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
                                tool_result_str = "timeout"
                                logger.warning("Tool '%s' timed out", tool_name)
                                tool_span.set_attribute("tool.is_error", True)
                            except Exception as exc:
                                result = MCPToolResult(
                                    content=[{"type": "text", "text": f"Tool '{tool_name}' error: {exc}"}],
                                    is_error=True,
                                )
                                tool_result_str = "error"
                                logger.warning("Tool '%s' error: %s", tool_name, exc)
                                tool_span.set_attribute("tool.is_error", True)

                        result_text = "\n".join(
                            c.get("text", "") for c in result.content if c.get("type") == "text"
                        )
                        if len(result_text) > trace_tool_result_max:
                            result_text = result_text[:trace_tool_result_max] + "… [truncated]"
                        await _emit({
                            "type": "tool_result",
                            "name": tool_name,
                            "content": result_text,
                            "is_error": result.is_error,
                        })
                        logger.debug(
                            "Agent %s — tool result: %s (error=%s, %d chars)",
                            agent.name, tool_name, result.is_error, len(result_text),
                        )

                        server_name = mcp_manager.get_server_for_tool(tool_name) or "unknown"
                        record_tool_call(
                            mcp_server=server_name,
                            tool=tool_name,
                            result=tool_result_str,
                            duration_s=time.monotonic() - tool_start,
                        )

                        if is_openai_compat:
                            return mcp_result_to_openrouter(tool_use_id, result)
                        else:
                            return mcp_result_to_anthropic(tool_use_id, result)

                    tool_calls_made += len(tool_use_blocks)
                    # gather() preserves input order in its results regardless of
                    # completion order, so message ordering stays deterministic.
                    tool_results: list[dict] = list(
                        await asyncio.gather(*(_execute_tool_call(b) for b in tool_use_blocks))
                    )

                    # Append tool results (Anthropic: batched in one user message;
                    # OpenRouter: individual tool messages)
                    if is_openai_compat:
                        messages.extend(tool_results)
                    else:
                        messages.append({"role": "user", "content": tool_results})

                    # ── Iteration guard ───────────────────────────────────────
                    if iterations >= limits.max_agent_iterations:
                        raise AgentRunError(
                            f"max iterations exceeded: {limits.max_agent_iterations}"
                        )
        except AgentRunError as exc:
            err_msg = str(exc)
            if "timed out" in err_msg:
                run_status = "timeout"
            elif "max iterations" in err_msg:
                run_status = "max_iterations"
            else:
                run_status = "error"
            raise
        except asyncio.CancelledError:
            # Caller (gateway WS handler) cancelled us — e.g. the client
            # disconnected mid-run. Not an error; record it distinctly so it
            # isn't misreported as a successful "ok" run.
            run_status = "aborted"
            raise
        except Exception:
            run_status = "error"
            raise
        finally:
            record_agent_run_complete(
                agent=agent.name,
                model=model_used,
                status=run_status,
                duration_s=time.monotonic() - start,
                iterations=iterations,
                tool_calls=tool_calls_made,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )
