# P-Ork Gateway — Claude Code Context

## What this is
A Python/FastAPI WebSocket gateway that is an optional alternative to OpenClaw as the
agent execution backend for P-Ork (https://github.com/bantex01/P-Ork).

## Current build stage
STAGE 5: Ollama + Google Providers

Stages 1-4 are COMPLETE. The gateway starts, handles auth, loads agents, manages sessions,
spawns MCP servers, caches tools, routes tool calls, and runs a full agentic loop with real
LLM calls via Anthropic and OpenRouter. Stage 5 adds Ollama and Google as additional providers.

## Running the gateway
uvicorn gateway.main:app --port 18789 --reload

## Key files (existing)
- gateway/main.py                — FastAPI app, WS endpoint, lifespan, auth, agent handler (stub)
- gateway/auth/device.py         — Identity generation and token auth
- gateway/agents/loader.py       — Agent YAML + soul.md loader with SIGHUP reload
- gateway/session/manager.py     — Session key registry with prefix validation
- gateway/mcp/transport.py       — MCP stdio JSON-RPC transport
- gateway/mcp/manager.py         — MCP child process manager, tool registry, call_tool
- gateway/models/config.py       — GatewayConfig with limits and ENV_VAR resolution
- gateway/models/agent.py        — AgentConfig Pydantic model
- config.yaml                    — Gateway configuration

## Key files (BUILD THIS STAGE)
- gateway/llm/tool_translator.py — MCP tool defs ↔ provider tool schema translation
- gateway/llm/router.py           — Model string → provider routing
- gateway/llm/providers/anthropic.py — Anthropic SDK client wrapper
- gateway/llm/providers/openrouter.py — OpenRouter via httpx (OpenAI-compat)
- gateway/runner/agent_runner.py    — Full agentic loop: LLM ↔ MCP tool calls

## STAGE 4 GOAL
Gateway can call LLMs with tools, run the full agentic loop, and return real agent
responses. After this stage, real pipeline execution works end-to-end.

## STAGE 4 DELIVERABLES

### 1. Tool schema translator (gateway/llm/tool_translator.py)

**mcp_to_anthropic(tools: list[MCPTool]) -> list[dict]**
Converts MCP tool definitions to Anthropic's tool format:
```python
{
    "name": tool.name,
    "description": tool.description,
    "input_schema": tool.input_schema   # MCP inputSchema maps directly
}
```

**mcp_to_openrouter(tools: list[MCPTool]) -> list[dict]**
Converts MCP tool definitions to OpenAI-compat format (used by OpenRouter):
```python
{
    "type": "function",
    "function": {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema
    }
}
```

**anthropic_tool_use_to_mcp(tool_use_block) -> tuple[str, dict]**
Extracts (tool_name, arguments_dict) from an Anthropic tool_use content block.

**openrouter_tool_call_to_mcp(tool_call) -> tuple[str, dict]**
Extracts (tool_name, arguments_dict) from an OpenRouter/OpenAI tool_call object.

**mcp_result_to_anthropic(tool_use_id, result: MCPToolResult) -> dict**
Converts an MCPToolResult to an Anthropic tool_result content block:
```python
{
    "type": "tool_result",
    "tool_use_id": tool_use_id,
    "content": result.content if not result.is_error else [{"type": "text", "text": error_text}],
    "is_error": result.is_error
}
```

**mcp_result_to_openrouter(tool_call_id, result: MCPToolResult) -> dict**
Converts an MCPToolResult to an OpenAI-compat tool role message:
```python
{
    "role": "tool",
    "tool_call_id": tool_call_id,
    "content": error_text if result.is_error else json.dumps(result.content)
}
```

### 2. Anthropic provider (gateway/llm/providers/anthropic.py)

Thin wrapper around `anthropic` SDK `AsyncAnthropic`.

```python
class AnthropicProvider(BaseProvider):
    async def complete(self, system: str, messages: list, tools: list[dict] | None,
                       model: str, max_tokens: int, thinking_level: str | None = None) -> ProviderResponse
```

- Uses `anthropic.AsyncAnthropic(api_key=...)` 
- Thinking level mapping (Anthropic only):
  - off → don't send thinking parameter
  - minimal → budget_tokens: 1024
  - low → budget_tokens: 2048
  - medium → budget_tokens: 4096
  - high → budget_tokens: 8192
  - xhigh → budget_tokens: 16384
- When thinking is enabled, set `thinking={"type": "enabled", "budget_tokens": N}` and bump max_tokens by budget_tokens
- ProviderResponse is a normalised model with: content_blocks, stop_reason, model_used, usage
- Raises ProviderError on API errors

### 3. OpenRouter provider (gateway/llm/providers/openrouter.py)

httpx.AsyncClient hitting `https://openrouter.ai/api/v1/chat/completions`.

```python
class OpenRouterProvider(BaseProvider):
    async def complete(self, system: str, messages: list, tools: list[dict] | None,
                       model: str, max_tokens: int, thinking_level: str | None = None) -> ProviderResponse
```

- Same `complete()` signature as Anthropic
- Non-streaming only in Phase 1 — simple POST, no SSE handling
- OpenRouter does NOT support extended thinking — log warning and ignore thinking_level if set
- Respects `config.limits.request_timeout_seconds` via httpx timeout
- Translates OpenAI-compat response to ProviderResponse

### 4. LLM router (gateway/llm/router.py)

```python
class LLMRouter:
    def get_provider(self, model_string: str) -> BaseProvider
```

- Model string prefix determines provider:
  - `anthropic/` → Anthropic provider (strip prefix, pass rest as model name)
  - `openrouter/` → OpenRouter provider (strip prefix, pass rest as model name)
  - No prefix → Anthropic provider (treat as bare model name)
- Provider instances are cached (one per provider type)
- Reads API keys from config.providers

### 5. Agent runner (gateway/runner/agent_runner.py)

The core agentic loop. This is the heart of the gateway.

```python
class AgentRunner:
    async def run(self, agent: AgentConfig, session_key: str, message: str,
                  model_override: str | None, thinking_level: str | None,
                  mcp_manager: MCPManager, limits: LimitsConfig) -> AgentRunResult
```

AgentRunResult:
```python
class AgentRunResult(BaseModel):
    text: str
    model_used: str
    duration_ms: int
    tool_calls_made: int
    iterations: int
```

The loop:
1. Look up agent config (model, soul, tools)
2. Apply model override if provided in params
3. Resolve tool list: agent.tools → MCP server instances → tool definitions
4. Translate MCP tool definitions → provider tool schema format
5. Build messages: `[{role: "user", content: prompt}]`
6. Send to LLM with `system=soul`, `tools=translated_tools`, `max_tokens=agent.max_tokens`
7. If LLM returns tool_use blocks:
   a. For each tool call:
      - Route to correct MCP server by tool name via mcp_manager.call_tool
      - Timeout: config.limits.mcp_tool_timeout_seconds
      - On MCP crash or timeout: return tool_result with is_error=true, continue
   b. Append assistant message (with tool_use) to messages
   c. Append tool_result message(s) to messages
   d. Increment iteration counter
   e. If counter >= limits.max_agent_iterations: raise AgentRunError("max iterations exceeded: {n}")
   f. Go to step 6
8. If LLM returns text with no tool calls: this is the final response, return as payload text

On max iterations exceeded: raise AgentRunError
On LLM timeout (request_timeout_seconds): raise AgentRunError("LLM request timed out after {n}s")

NO streaming events in Phase 1. The runner returns a complete result when done.

### 6. Update WS agent handler (gateway/main.py)

Replace the stub response with real agent runner:

- Import and instantiate AgentRunner
- On `agent` method: look up agent, validate session key (already done)
- Send accepted frame immediately (before runner starts)
- Run agent_runner.run() as async task
- On completion: send ok frame with full result formatted as per WS protocol:
  ```json
  {
    "type": "res", "id": "<same-uuid>", "ok": true,
    "payload": {
      "status": "ok",
      "result": {
        "payloads": [{"text": "<agent response text>", "mediaUrl": null}],
        "meta": {
          "durationMs": 8503,
          "agentMeta": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6-20250514",
            "usage": {"input_tokens": 1234, "output_tokens": 456}
          },
          "aborted": false
        }
      }
    }
  }
  ```
- On error: send `{"type": "res", "id": "<same-uuid>", "ok": false, "error": {"message": "..."}}`

## BaseProvider interface (gateway/llm/providers/base.py)

Create a base class:
```python
from abc import ABC, abstractmethod

class BaseProvider(ABC):
    @abstractmethod
    async def complete(self, system: str, messages: list, tools: list[dict] | None,
                      model: str, max_tokens: int, thinking_level: str | None = None) -> ProviderResponse:
        ...
```

ProviderResponse (can go in the same file or in models/):
```python
class ProviderResponse(BaseModel):
    content_blocks: list[dict]    # raw content blocks from the LLM
    stop_reason: str              # "end_turn", "tool_use", "max_tokens", etc.
    model_used: str               # actual model name returned by API
    usage: dict                   # {"input_tokens": N, "output_tokens": N}
```

## Dependency additions
anthropic>=0.40.0
httpx>=0.27.0

(These are already in requirements.txt)

## Test milestone
Configure a real agent with a real Anthropic API key and at least one MCP tool.
Run a P-Ork pipeline or use a direct WS test script that:
1. Connects, authenticates
2. Sends an agent request for sre-triage-sonnet
3. Receives accepted frame, then ok frame
4. The response contains real LLM output (not a stub)
5. If the agent makes a tool call, verify it goes through the MCP manager

Also test model override: send an agent request with model: "anthropic/claude-haiku-4-5-20251001"
and verify the gateway uses Haiku instead of the agent's default Sonnet.

## Auth (Phase 1: token-only)
Token-only. Challenge/nonce sent but not verified.

## WS Protocol (critical)
The agent method sends TWO res frames with the same id:
  1. {status: "accepted", runId: "..."}   — immediate, before agent runs
  2. {status: "ok", result: {...}}         — final, after agent completes
No streaming events in Phase 1.

## Runtime limits
- max_agent_iterations: 20 (from config)
- request_timeout_seconds: 180 (from config, used for LLM API calls)
- mcp_tool_timeout_seconds: 30 (from config, used for MCP tool calls)

## Known issues
- MCP servers do not hot-reload — adding a new MCP server requires restart
- OpenRouter non-streaming only — SSE deferred until P-Ork needs it
- Ed25519 not active until Stage 5
- Sessions in-memory — restart clears history (intentional)