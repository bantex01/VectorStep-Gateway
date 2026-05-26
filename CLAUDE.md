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
STAGE 3: MCP Process Manager

Stages 1-2 are COMPLETE. The gateway starts, handles auth, loads agents, validates session
keys, and returns stub LLMOutput responses for agent requests.

## Running the gateway
uvicorn gateway.main:app --port 18789 --reload

## Key files (existing)
- gateway/main.py                — FastAPI app, WS endpoint, lifespan, auth, agent handler
- gateway/auth/device.py         — Identity generation and token auth
- gateway/agents/loader.py       — Agent YAML + soul.md loader with SIGHUP reload
- gateway/session/manager.py     — Session key registry with prefix validation
- gateway/models/config.py       — GatewayConfig with limits and ENV_VAR resolution
- gateway/models/agent.py        — AgentConfig Pydantic model
- config.yaml                    — Gateway configuration
- agents/                        — Agent definitions (sre-triage-sonnet exists)

## Key files (BUILD THIS STAGE)
- gateway/mcp/transport.py       — MCP stdio JSON-RPC transport
- gateway/mcp/manager.py         — Spawns/monitors MCP child processes, tools/list, tools/call

## STAGE 3 GOAL
Gateway spawns and manages MCP server child processes, can list available tools from each,
and routes tool calls to the right server. No LLM integration yet — just the MCP layer
working in isolation.

## STAGE 3 DELIVERABLES

### 1. MCP stdio transport (gateway/mcp/transport.py)

Wraps a subprocess with stdin/stdout pipes. Implements JSON-RPC 2.0 framing over stdio
(newline-delimited JSON).

Methods:
- `async start()` — spawn subprocess, start background reader
- `async initialize()` — send initialize request, receive response, send initialized notification
  - initialize request params: `{"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "pork-gateway", "version": "0.1.0"}}`
- `async send_request(method, params)` → awaitable response (matches by id)
- `async send_notification(method, params)` → fire and forget (no id, no response)
- `async call_tool(name, arguments)` → calls tools/call and returns result
- `async list_tools()` → calls tools/list and returns tool definitions
- `async stop()` — send SIGTERM, wait for exit, clean up
- Background reader task draining stdout continuously, dispatching responses to waiting futures

The transport must handle:
- Newline-delimited JSON (each line is a complete JSON-RPC message)
- Matching responses to requests by id
- Timeout on requests (use config.limits.mcp_tool_timeout_seconds for tools/call, 10s for initialize)
- Clean shutdown (stop reader task, close subprocess stdin, SIGTERM, wait)

### 2. MCP manager (gateway/mcp/manager.py)

Reads `mcp_servers` from config at startup. For each server config entry:
- Spawn subprocess with `asyncio.create_subprocess_exec`
- Resolve `${ENV_VAR}` in env blocks (same logic as config.py)
- Initialize each server (call transport.initialize())
- Call tools/list and cache tool definitions
- Store MCPTool objects keyed by tool name with server_name attribute

Methods:
- `async start_all()` — spawn, initialize, and cache tools for all configured servers
- `async stop_all()` — graceful shutdown of all servers
- `get_tools_for_agent(agent: AgentConfig) -> list[MCPTool]` — filter global tools by agent's `tools:` list
- `async call_tool(tool_name, arguments) -> MCPToolResult` — route to correct server, send tools/call
- Health monitoring: restart crashed servers (simple restart, no backoff in Phase 1)
  - Monitor subprocess return codes; if a server exits unexpectedly, log and restart
  - On restart: re-initialize and re-cache tools
- `get_server_status() -> dict` — return status of each server (name, running, pid, restart_count)

Internal models:
```python
class MCPTool(BaseModel):
    name: str
    description: str
    input_schema: dict       # JSON Schema object (from MCP inputSchema)
    server_name: str         # which MCP server owns this tool

class MCPToolResult(BaseModel):
    content: list[dict]      # MCP content blocks
    is_error: bool = False
```

### 3. Wire into lifespan (gateway/main.py)

Update the lifespan handler:
- After loading agents, start MCP manager (start_all)
- On shutdown: stop MCP manager (stop_all)
- Log loaded tools at startup: number of tools per server
- Add MCP manager to _state dict for use in WS handler and HTTP endpoints

### 4. HTTP endpoints

- `GET /mcp/tools` — returns all loaded tool definitions grouped by server:
  ```json
  {
    "filesystem": [
      {"name": "read_file", "description": "...", "inputSchema": {...}},
      {"name": "write_file", "description": "...", "inputSchema": {...}}
    ]
  }
  ```
- `GET /mcp/servers` — return server status (name, running, pid, restart_count):
  ```json
  {
    "filesystem": {"running": true, "pid": 12345, "restart_count": 0}
  }
  ```

### 5. Tool name collision handling

If two MCP servers expose a tool with the same name, the gateway must disambiguate.
Strategy: last-registered wins, with a startup warning logged. Tool names are scoped to
a specific server in the internal registry.

### 6. Config for testing

The config.yaml already has a filesystem MCP server entry. For testing, you may want to
adjust the path argument from `/home/user` to a local temp directory, e.g.:
```yaml
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/pork-gw-test"]
```

## MCP protocol notes
- MCP uses JSON-RPC 2.0 over stdio with newline-delimited messages
- initialize request params: `{"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "pork-gateway", "version": "0.1.0"}}`
- tools/list response: `{"tools": [{"name": "...", "description": "...", "inputSchema": {...}}]}`
- tools/call request params: `{"name": "tool_name", "arguments": {...}}`
- tools/call response: `{"content": [{"type": "text", "text": "..."}], "isError": false}`

## Runtime limits (from config.yaml limits block)
- max_agent_iterations: 20    — LLM ↔ tool call loop cap (not used yet)
- request_timeout_seconds: 180 — single LLM API call timeout (not used yet)
- mcp_tool_timeout_seconds: 30 — per MCP tool call timeout (USED THIS STAGE)

## Known issues
- MCP servers do not hot-reload — adding a new MCP server requires restart
- OpenRouter uses non-streaming POST only — SSE deferred until P-Ork needs it
- Ed25519 signature verification not active until Stage 5
- Sessions are in-memory — gateway restart clears all session history (intentional)

## Test milestone
After building, verify:
1. Start gateway with filesystem MCP server configured
2. Check startup logs show MCP initialisation handshake and tool caching
3. Hit `GET /mcp/tools` — should return tool list from filesystem server (read_file, write_file, etc.)
4. Hit `GET /mcp/servers` — should show filesystem server as running with pid
5. Write a quick test that calls `manager.call_tool("read_file", {"path": "/tmp/pork-gw-test/test.txt"})`
   after creating that file, and verify the content comes back
6. Kill the filesystem MCP server process — verify manager detects crash, restarts, and
   next call_tool still works
7. Hit `GET /mcp/tools` after restart — tools still available