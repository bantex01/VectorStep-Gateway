# P-Ork Gateway — Claude Code Context

## What this is
A Python/FastAPI WebSocket gateway that is an optional alternative to OpenClaw as the
agent execution backend for P-Ork (https://github.com/bantex01/P-Ork).
P-Ork users who want to run this gateway use `executor: pork_gateway` in their pipeline
YAML. Users who prefer OpenClaw continue to use `executor: openclaw` — both coexist.

The gateway speaks the same WebSocket RPC protocol as OpenClaw's Gateway API.
P-Ork's new `PorkGatewayWSExecutor` (service/src/executors/pork_gateway_ws.py) connects
to this gateway. The existing `OpenClawWSExecutor` is unchanged.

## Current build stage
STAGE 2: Agent Loader + Session Manager

Stage 1 is COMPLETE. The gateway starts, generates identity, and handles WebSocket auth
(token-only). Agent requests return "not implemented".

## Running the gateway
uvicorn gateway.main:app --port 18789 --reload

## Key files
- gateway/main.py                — FastAPI app, WS endpoint, lifespan (DONE)
- gateway/auth/device.py         — Identity generation and token auth (DONE)
- gateway/agents/loader.py       — Agent YAML loader (BUILD THIS STAGE)
- gateway/session/manager.py     — Session key registry (BUILD THIS STAGE)
- gateway/models/config.py       — GatewayConfig Pydantic model (DONE)
- gateway/models/agent.py        — AgentConfig Pydantic model (DONE)
- config.yaml                    — Gateway configuration (DONE)
- agents/                        — Agent definitions (sre-triage-sonnet exists)

## STAGE 2 GOAL
Gateway can load agent configs from YAML, validate them, and manage session isolation.
Agent calls now return a stub response in the correct LLMOutput JSON format (instead of
"not implemented").

## STAGE 2 DELIVERABLES

### 1. AgentConfig model (gateway/models/agent.py) — already exists, verify it matches:

```python
class AgentConfig(BaseModel):
    name: str
    model: str
    max_tokens: int = 8192
    tools: list[str] = []
    soul: str = ""  # loaded content of soul.md
```

### 2. Agent loader (gateway/agents/loader.py)

- Globs `agents/*/agent.yaml` from configured `agents_dir`
- Reads `soul.md` from same directory (file must exist, fail validation if missing)
- Validates via AgentConfig
- Returns `dict[str, AgentConfig]` keyed by agent name
- Agent name in YAML must match directory name — validation error if not
- Hot-reload on SIGHUP (consistent with P-Ork's own reload pattern)
- `POST /reload` endpoint also triggers agent reload

### 3. Session manager (gateway/session/manager.py)

- Registry of active session keys → session state
- Session state: `{session_key, agent_name, messages: list, created_at}`
- Session key validation: must start with `agent:{agentId}:` — reject with error if not
  Only validate the PREFIX. Everything after `agent:{agentId}:` is free-form.
  Example valid key: `agent:sre-triage-sonnet:pipeline:run-123:triage`
  Example invalid key: `agent:wrong-agent:some-run:triage` (agent name doesn't match)
- Session isolation: each unique session key gets its own message history
- Sessions are in-memory (no persistence needed in Phase 1)
- `get_or_create(session_key, agent_name)` method — returns existing session or creates new

### 4. Agent handler update (gateway/main.py)

Update the `agent` method handler in the WS endpoint:

- Look up agent by `agentId` from params — return error if agent not found:
  `{"ok": false, "error": {"message": "agent not found: {agentId}"}}`
- Validate session key format — return error if prefix doesn't match:
  ```python
  expected_prefix = f"agent:{agent_id}:"
  if not session_key.startswith(expected_prefix):
      return {"ok": false, "error": {"message": f"sessionKey must start with '{expected_prefix}', got: {session_key}"}}
  ```
- Return stub LLMOutput-shaped JSON wrapped in the correct two-frame protocol:
  
  Frame 1 (immediately):
  ```json
  {"type": "res", "id": "<same-uuid>", "ok": true, "payload": {"status": "accepted", "runId": "<uuid>"}}
  ```
  
  Frame 2 (after short processing):
  ```json
  {
    "type": "res",
    "id": "<same-uuid>",
    "ok": true,
    "payload": {
      "status": "ok",
      "result": {
        "payloads": [{"text": "{\"confidence\": 1.0, \"summary\": \"stub response from gateway\", \"next_step_context\": \"\", \"proceed\": true}", "mediaUrl": null}],
        "meta": {
          "durationMs": 1,
          "agentMeta": {
            "provider": "stub",
            "model": "<agent.model>",
            "usage": {"input_tokens": 0, "output_tokens": 0}
          },
          "aborted": false
        }
      }
    }
  }
  ```

  CRITICAL: Both frames MUST use the same request ID from the agent request.
  P-Ork's executor loops waiting for the second frame with matching ID.

### 5. Health endpoints

- `GET /agents` — returns list of loaded agent names and their models:
  ```json
  {"agents": [{"name": "sre-triage-sonnet", "model": "anthropic/claude-sonnet-4-6"}]}
  ```
- `POST /reload` — reloads agent configs from disk, returns updated agent list

### 6. Wire into lifespan

Update the lifespan handler in main.py to:
- Load agents on startup (call the agent loader)
- Log loaded agents at startup
- On SIGHUP: reload agents

## Auth (Phase 1: token-only)
The gateway validates the operator token from params.auth.token on connect.
Token is stored in device-auth.json (loaded at startup).
The challenge/nonce flow is performed but nonce is NOT verified in Phase 1.
Full Ed25519 signature verification is added in Stage 5.

## WS Protocol (critical)
The agent method sends TWO res frames with the same id:
  1. `{"status": "accepted", "runId": "..."}` — sent immediately, before agent runs
  2. `{"status": "ok", "result": {...}}` — sent after agent completes
P-Ork's executor loops waiting for the second frame. Both MUST use the original request id.

## Session key validation
Validate that sessionKey starts with `agent:{agentId}:` — PREFIX ONLY.
Everything after that third colon is free-form. Do NOT attempt to parse or validate the suffix.
Reject with a clear error if the prefix is wrong so the P-Ork step fails with a legible message.

## Runtime limits (from config.yaml limits block)
- max_agent_iterations: 20    — LLM ↔ tool call loop cap
- request_timeout_seconds: 180 — single LLM API call timeout
- mcp_tool_timeout_seconds: 30 — per MCP tool call timeout

## Known issues
- MCP servers do not hot-reload — adding a new MCP server requires restart (not relevant yet)
- OpenRouter uses non-streaming POST only — SSE deferred until P-Ork needs it
- Ed25519 signature verification not active until Stage 5
- Sessions are in-memory — gateway restart clears all session history (intentional)

## Test milestone
After building, verify:
1. Create a test agent: `mkdir -p agents/test-agent && echo 'name: test-agent\nmodel: anthropic/claude-haiku-4-5-20251001\ntools: []' > agents/test-agent/agent.yaml && echo "You are a test agent." > agents/test-agent/soul.md`
2. Start gateway — should log loaded agents
3. `GET /agents` — returns list of agents with models
4. WS: agent request with valid agent → stub LLMOutput response (two frames: accepted then ok)
5. WS: agent request with invalid agent → "agent not found" error
6. WS: agent request with wrong session key prefix → "sessionKey must start with..." error
7. `POST /reload` — reloads agents, returns updated list
8. SIGHUP — reloads agents (check logs)