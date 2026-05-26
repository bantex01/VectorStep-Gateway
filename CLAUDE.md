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
STAGE 1: Project Skeleton + Token Auth

## Running the gateway
uvicorn gateway.main:app --port 18789 --reload

## Key files (all to be created in this stage)
- gateway/main.py                — FastAPI app, WS endpoint, lifespan
- gateway/auth/device.py         — Identity generation and token auth validation
- gateway/models/config.py       — GatewayConfig, LimitsConfig, etc. Pydantic models
- gateway/models/agent.py        — AgentConfig Pydantic model (stub for now)
- config.yaml                    — Gateway configuration (already exists)
- agents/                        — Agent definitions (already exists with sample agent)

## STAGE 1 GOAL
A running WebSocket server that completes the auth handshake using token-only validation
and rejects invalid tokens. The challenge/nonce flow is performed (because
PorkGatewayWSExecutor sends it) but nonce verification is skipped. Agent calls return a
"not implemented" error. 

## STAGE 1 DELIVERABLES

### 1. Config loading (gateway/models/config.py)

Load config.yaml with ${ENV_VAR} resolution. All fields must be typed Pydantic models:

```python
class LimitsConfig(BaseModel):
    max_agent_iterations: int = 20
    request_timeout_seconds: int = 180
    mcp_tool_timeout_seconds: int = 30

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 18789

class IdentityConfig(BaseModel):
    path: str = "~/.pork-gateway/identity"

class MCPServerConfig(BaseModel):
    command: str
    args: list[str] = []
    env: dict[str, str] = {}

class ProviderConfig(BaseModel):
    api_key: str = ""
    base_url: str | None = None

class ProvidersConfig(BaseModel):
    anthropic: ProviderConfig = ProviderConfig()
    openrouter: ProviderConfig = ProviderConfig()

class LoggingConfig(BaseModel):
    level: str = "INFO"

class GatewayConfig(BaseModel):
    server: ServerConfig
    agents_dir: str = "./agents"
    identity: IdentityConfig
    limits: LimitsConfig = LimitsConfig()
    mcp_servers: dict[str, MCPServerConfig] = {}
    providers: ProvidersConfig
    logging: LoggingConfig = LoggingConfig()
```

ENV_VAR resolution: scan all string values for `${VAR_NAME}` patterns and replace with
os.environ.get("VAR_NAME", ""). Do this recursively on the raw YAML dict before passing
to Pydantic.

### 2. Identity generation (gateway/auth/device.py)

On first run, if no identity files exist at config.identity.path:

- Generate a UUID device ID
- Generate a UUID operator token
- Write `device.json`: `{"deviceId": "<uuid>", "privateKeyPem": ""}`
  (PEM field present but empty — populated in Stage 5)
- Write `device-auth.json`: 
  `{"tokens": {"operator": {"token": "<uuid>", "scopes": ["agent:invoke", "gateway:connect"]}}}`
- Both written to the configured identity path directory (create dir if needed)
- Print first-run bootstrap message:
  ```
  [pork-gateway] First run — generated device identity.
  [pork-gateway] Written to: <path>/device.json
  [pork-gateway] Written to: <path>/device-auth.json
  [pork-gateway] Add the following to your P-Ork config.yaml:
  [pork-gateway]   executors:
  [pork-gateway]     pork_gateway:
  [pork-gateway]       url: ws://localhost:18789/rpc
  [pork-gateway]       identity_dir: <path>
  [pork-gateway] Then use executor: pork_gateway in your pipeline YAML steps.
  ```
- Identity dir configurable via PORK_GATEWAY_IDENTITY_DIR env var (overrides config)

On subsequent runs: load existing device.json and device-auth.json.

### 3. WebSocket endpoint (gateway/main.py)

FastAPI app with lifespan handler that:
1. Loads config.yaml
2. Generates/loads identity
3. Sets up logging

WebSocket endpoint at `/rpc`:

**On connect:**
- Send challenge event immediately:
  ```json
  {"type": "event", "event": "challenge", "payload": {"nonce": "<random-string>"}}
  ```

**Handle connect request:**
- Validate `params.auth.token` against loaded operator token
- If valid: return `{"type": "res", "id": "<same-uuid>", "ok": true, "payload": {"protocol": 3}}`
- If invalid: return `{"type": "res", "id": "<same-uuid>", "ok": false, "error": {"message": "invalid operator token"}}`

**Handle agent request (after auth):**
- Return `{"type": "res", "id": "<same-uuid>", "ok": false, "error": {"message": "not implemented"}}`

**All other methods:**
- Return `{"type": "res", "id": "<same-uuid>", "ok": false, "error": {"message": "unknown method: <method>"}}`

### 4. Stub files

Create minimal stubs for files that will be populated in later stages:
- gateway/agents/loader.py — empty or with a TODO comment
- gateway/mcp/manager.py — empty or with a TODO comment
- gateway/mcp/transport.py — empty or with a TODO comment
- gateway/llm/router.py — empty or with a TODO comment
- gateway/llm/tool_translator.py — empty or with a TODO comment
- gateway/llm/providers/anthropic.py — empty or with a TODO comment
- gateway/llm/providers/openrouter.py — empty or with a TODO comment
- gateway/runner/agent_runner.py — empty or with a TODO comment
- gateway/session/manager.py — empty or with a TODO comment
- gateway/models/agent.py — AgentConfig Pydantic model (define the schema even though it's not used yet)

### 5. AgentConfig model (gateway/models/agent.py)

Define the schema even though Stage 1 doesn't use it:
```python
class AgentConfig(BaseModel):
    name: str
    model: str
    max_tokens: int = 8192
    tools: list[str] = []
    soul: str = ""  # loaded content of soul.md
```

## WS Protocol details (critical)

Message format:
- Client → Server: `{"type": "req", "id": "<uuid>", "method": "<method>", "params": {...}}`
- Server → Client: `{"type": "res", "id": "<uuid>", "ok": true/false, "payload": {...}}`
- Server → Client: `{"type": "event", "event": "<name>", "payload": {...}}`

The `id` field in every response MUST match the `id` from the request. This is critical —
P-Ork's executor matches responses by id.

The connect request includes device fields (publicKey, signature, signedAt, nonce) but
these are IGNORED in Phase 1 — only auth.token is validated.

## Auth (Phase 1: token-only)
The gateway validates the operator token from params.auth.token on connect.
Token is stored in device-auth.json (loaded at startup).
The challenge/nonce flow is performed but nonce is NOT verified in Phase 1.
Full Ed25519 signature verification is added in Stage 5.

## Test milestone
After building, verify:
1. `uvicorn gateway.main:app --port 18789` starts without error
2. First-run generates identity files and prints bootstrap message
3. WS client connects, receives challenge event
4. Valid token → auth accepted
5. Invalid token → auth rejected
6. Agent request → "not implemented" error
7. Unknown method → "unknown method" error

## Dependencies (Stage 1 only)
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
websockets>=12.0
pydantic>=2.0
pyyaml>=6.0

Note: cryptography NOT needed until Stage 5. anthropic and httpx NOT needed until Stage 4.

## Known issues
- Ed25519 signature verification not active until Stage 5
- MCP servers do not hot-reload — adding a new MCP server requires restart (not relevant yet)
- Sessions are in-memory — gateway restart clears all session history (intentional, not relevant yet)
- No streaming events in Phase 1