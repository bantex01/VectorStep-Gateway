# P-Ork Gateway Service

A Python/FastAPI WebSocket gateway that runs AI agents with MCP tools, replacing OpenClaw as the executor backend for P-Ork pipelines.

## Quick Start

```bash
# Install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Set required environment variables (at least one LLM provider)
export ANTHROPIC_API_KEY=sk-ant-...     # for Anthropic provider
# export OPENROUTER_API_KEY=...        # for OpenRouter
# export OLLAMA_API_KEY=...             # for Ollama cloud
# export GOOGLE_API_KEY=...             # for Google Gemini

# Start the gateway
.venv/bin/uvicorn gateway.main:app --port 18789

# The auth token is auto-generated on first run.
# Find it in ~/.pork-gateway/identity/device-auth.json
# Use the 'operator' token value for P-Ork's executors.gateway.token config.
```

The gateway auto-generates an identity (device key + operator token) on first run. The operator token is stored in `~/.pork-gateway/identity/device-auth.json`.

## Directory Structure

```
P-Ork-Gateway/
├── config.yaml                  # Gateway configuration (providers, MCP servers, limits)
├── agents/                       # Agent definitions (one directory per agent)
│   ├── sre-triage-sonnet/
│   │   ├── agent.yaml            # Agent config (model, tools, max_tokens)
│   │   └── soul.md              # System prompt / personality
│   └── test-ollama/
│       ├── agent.yaml
│       └── soul.md
├── gateway/                     # Gateway source code
│   ├── main.py                  # FastAPI app, WS endpoint, auth, agent handler
│   ├── auth/device.py           # Identity generation, token auth
│   ├── agents/loader.py         # Agent YAML + soul.md loader with SIGHUP reload
│   ├── session/manager.py       # Session key registry (prefix validation, isolation)
│   ├── mcp/
│   │   ├── transport.py         # MCP stdio JSON-RPC transport
│   │   └── manager.py          # MCP process manager, tool registry, call routing
│   ├── llm/
│   │   ├── router.py            # Model string → provider routing
│   │   ├── tool_translator.py   # MCP tool schemas ↔ provider format translation
│   │   ├── providers/
│   │   │   ├── base.py          # BaseProvider ABC, ProviderResponse
│   │   │   ├── anthropic.py     # Anthropic API (native SDK)
│   │   │   ├── openrouter.py    # OpenRouter via httpx (OpenAI-compat)
│   │   │   ├── ollama.py        # Ollama local or cloud (OpenAI-compat)
│   │   │   └── google.py       # Google Gemini via OpenAI-compat endpoint
│   │   └── runner/
│   │       └── agent_runner.py  # Full agentic loop: LLM ↔ MCP tool calls
│   └── models/
│       ├── config.py            # GatewayConfig, provider configs, limits
│       └── agent.py             # AgentConfig Pydantic model
└── requirements.txt
```

## Configuration Reference (`config.yaml`)

### Full Example

```yaml
server:
  host: 0.0.0.0
  port: 18780           # Different from OpenClaw (18789) to avoid collision

agents_dir: ./agents       # directory containing agent subdirectories

identity:
  path: ~/.pork-gateway/identity   # auto-generated on first run if absent

# Runtime limits
limits:
  max_agent_iterations: 20         # max LLM ↔ tool call loops per agent run
  request_timeout_seconds: 180     # how long to wait for a single LLM API response
  mcp_tool_timeout_seconds: 30     # per-tool-call timeout against MCP servers

# MCP tool servers — subprocesses the gateway spawns and manages
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  grafana:
    command: npx
    args: ["-y", "@grafana/mcp-grafana"]
    env:
      GRAFANA_URL: ${GRAFANA_URL}
      GRAFANA_TOKEN: ${GRAFANA_TOKEN}
  tavily:
    command: npx
    args: ["-y", "tavily-mcp"]
    env:
      TAVILY_API_KEY: ${TAVILY_API_KEY}

# LLM providers — each gets a model prefix for routing
providers:
  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
  openrouter:
    api_key: ${OPENROUTER_API_KEY}
    base_url: https://openrouter.ai/api/v1
  ollama:                          # local Ollama (e.g. on mm2) — no API key needed
    base_url: http://adalton-mm2:11434/v1
  ollama-cloud:                     # Ollama cloud (ollama.com) — needs API key
    api_key: ${OLLAMA_API_KEY}
    base_url: https://ollama.com/api
  google:                          # Google Gemini via OpenAI-compat endpoint
    api_key: ${GOOGLE_API_KEY}
    base_url: https://generativelanguage.googleapis.com/v1beta/openai

logging:
  level: INFO
```

### Config Sections

#### `server`

| Field | Default | Description |
|-------|---------|-------------|
| `host` | `0.0.0.0` | Bind address |
| `port` | `18789` | Bind port (same as OpenClaw — change if running both on same machine) |

#### `identity`

| Field | Default | Description |
|-------|---------|-------------|
| `path` | `~/.pork-gateway/identity` | Where identity files are stored. Auto-generated on first run. |

The operator token (used by P-Ork to authenticate with the gateway) is generated and stored in `<path>/device-auth.json`.

#### `limits`

| Field | Default | Description |
|-------|---------|-------------|
| `max_agent_iterations` | `20` | Max LLM ↔ tool call loops before aborting |
| `request_timeout_seconds` | `180` | Timeout for a single LLM API call |
| `mcp_tool_timeout_seconds` | `30` | Timeout for a single MCP tool call |

#### `mcp_servers`

Each key is a server name. The gateway spawns each as a subprocess using stdio JSON-RPC transport.

| Field | Required | Description |
|-------|----------|-------------|
| `command` | Yes | Executable to run (e.g. `npx`, `python3`) |
| `args` | No | Arguments to pass |
| `env` | No | Environment variables for the subprocess (supports `${VAR_NAME}` resolution) |

#### `providers`

Each key becomes a model prefix. Every provider has the same config shape:

| Field | Required | Description |
|-------|----------|-------------|
| `api_key` | No | API key. Env vars resolved via `${VAR_NAME}`. Empty = no auth header sent. |
| `base_url` | No | Override the default endpoint URL. |

**Default `base_url` values per provider:**

| Provider | Default `base_url` | Auth |
|----------|-------------------|------|
| `anthropic` | (SDK default) | `x-api-key` header (SDK handles this) |
| `openrouter` | `https://openrouter.ai/api/v1` | `Authorization: Bearer` |
| `ollama` | `http://localhost:11434/v1` | None (or Bearer if `api_key` set) |
| `ollama-cloud` | `https://ollama.com/api` | `Authorization: Bearer` |
| `google` | `https://generativelanguage.googleapis.com/v1beta/openai` | `x-goog-api-key` header |

## Model Routing

Model strings determine which provider handles the request. Use a prefix:

| Model String | Provider | Example |
|--------------|----------|---------|
| `anthropic/claude-sonnet-4-6-20250514` | Anthropic (direct SDK) | Native tool calling, extended thinking |
| `openrouter/deepseek-chat` | OpenRouter | OpenAI-compat, no thinking |
| `ollama/qwen3.5:4b` | Local Ollama | Free, on your hardware |
| `ollama-cloud/glm-5.1:cloud` | Ollama Cloud | Paid, hosted models |
| `google/gemini-2.0-flash` | Google Gemini | OpenAI-compat endpoint |
| `claude-sonnet-4-6-20250514` | Anthropic (no prefix = default) | Bare names default to Anthropic |

## Creating Agents

Agents live as subdirectories under `agents_dir` (default: `./agents/`).

### Agent Directory Structure

```
agents/
└── my-agent/           # directory name can be anything
    ├── agent.yaml      # required — agent configuration
    └── soul.md         # required — system prompt / personality
```

### `agent.yaml`

```yaml
name: my-agent                   # must match directory name
model: ollama-cloud/glm-5.1:cloud  # model string (prefix/bare name)
max_tokens: 4096                 # max output tokens per LLM call
tools:                            # MCP server names the agent can use
  - filesystem
  - tavily
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Agent name. Must match the directory name. |
| `model` | Yes | Default model string. Can be overridden per-request. |
| `max_tokens` | Yes | Max output tokens per LLM call. |
| `tools` | No | List of MCP server names (from `mcp_servers` config). Agent gets all tools from listed servers. |

### `soul.md`

The system prompt / personality for the agent. Written in Markdown. This is sent as the `system` message to the LLM.

```markdown
You are an SRE triage agent. Your job is to assess alerts and recommend actions.

Always return structured JSON with:
- confidence (0.0-1.0)
- summary
- next_step_context
- reasoning (supports, contradicts, assumptions)
```

### Hot Reload

Send `POST /reload` or `SIGHUP` to the gateway process to reload agent configs without restarting.

## WebSocket Protocol

P-Ork (or any client) connects to the gateway at `ws://<host>:<port>/rpc`.

### Authentication Flow

1. Client connects → gateway sends `challenge` event with a nonce
2. Client sends `connect` request with auth token
3. Gateway responds with protocol version

```json
// Gateway → Client (automatic on connect)
{"type": "event", "event": "challenge", "payload": {"nonce": "abc123..."}}

// Client → Gateway
{"type": "req", "id": "uuid-1", "method": "connect", "params": {"auth": {"token": "your-operator-token"}}}

// Gateway → Client
{"type": "res", "id": "uuid-1", "ok": true, "payload": {"protocol": 3}}
```

### Agent Request

```json
// Client → Gateway
{
  "type": "req",
  "id": "uuid-2",
  "method": "agent",
  "params": {
    "agentId": "my-agent",
    "sessionKey": "agent:my-agent:pipeline:run-123:step-1",
    "message": "Assess this alert: ...",
    "model": "ollama-cloud/glm-5.1:cloud",   // optional — overrides agent default
    "thinkingLevel": "medium"                // optional — Anthropic only
  }
}

// Gateway → Client (Frame 1: accepted immediately)
{"type": "res", "id": "uuid-2", "ok": true, "payload": {"status": "accepted", "runId": "uuid-3"}}

// Gateway → Client (Frame 2: final result)
{
  "type": "res",
  "id": "uuid-2",
  "ok": true,
  "payload": {
    "status": "ok",
    "result": {
      "payloads": [{"text": "Agent response text here", "mediaUrl": null}],
      "meta": {
        "durationMs": 8503,
        "agentMeta": {
          "provider": "ollama-cloud",
          "model": "glm-5.1:cloud",
          "usage": {"input_tokens": 1234, "output_tokens": 456}
        },
        "aborted": false
      }
    }
  }
}
```

### Session Keys

Session keys must start with `agent:<agentId>:` — this enforces isolation between agents.

- ✅ `agent:my-agent:pipeline:run-123:step-1`
- ✅ `agent:order-intake:pipeline:abc:order-intake`
- ❌ `pipeline:run-123:step-1` (missing agent prefix)

## REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/agents` | List loaded agents (name + model) |
| `POST` | `/reload` | Reload agent configs from disk |
| `GET` | `/mcp/tools` | List all MCP tools across all servers |
| `GET` | `/mcp/servers` | List MCP server status (pid, tool count) |

## Environment Variables

The gateway resolves `${VAR_NAME}` patterns in `config.yaml` at load time. Common ones:

| Variable | Used By | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | `providers.anthropic` | Anthropic API key |
| `OPENROUTER_API_KEY` | `providers.openrouter` | OpenRouter API key |
| `OLLAMA_API_KEY` | `providers.ollama-cloud` | Ollama cloud API key (get from https://ollama.com/settings/keys) |
| `GOOGLE_API_KEY` | `providers.google` | Google AI API key |
| `GRAFANA_URL` | `mcp_servers.grafana` | Grafana instance URL |
| `GRAFANA_TOKEN` | `mcp_servers.grafana` | Grafana API token |
| `TAVILY_API_KEY` | `mcp_servers.tavily` | Tavily search API key |
| `PORK_GATEWAY_CONFIG` | Gateway startup | Override config file path (default: `config.yaml`) |

## P-Ork Integration

P-Ork uses the `GatewayExecutor` to talk to the gateway. Configure in P-Ork's `config.yaml`:

```yaml
executors:
  gateway:
    url: ws://localhost:18789/rpc     # gateway WebSocket endpoint
    token: ${PORK_GATEWAY_TOKEN}       # operator token from device-auth.json
```

Then in a pipeline YAML, use `executor: gateway` instead of `executor: openclaw`:

```yaml
steps:
  - name: triage
    executor: gateway                    # uses GatewayExecutor
    executor_config:
      agent: sre-triage-sonnet           # gateway agent name
      model: ollama-cloud/glm-5.1:cloud  # optional model override
    prompt_template: |
      Assess this alert: {{summary}}
```

**Key advantage over OpenClaw executor:** No session file clearing hack. The gateway handles session isolation natively — each pipeline run gets a clean session.

## Building Stages (Completed)

| Stage | Description | Status |
|-------|-------------|--------|
| 1 | Skeleton + Token Auth | ✅ |
| 2 | Agent Loader + Session Manager | ✅ |
| 3 | MCP Process Manager | ✅ |
| 4 | LLM Executor + Tool Translation | ✅ |
| 5a | Ollama + Google + Ollama-Cloud providers | ✅ |
| 5b | Hardening (Ed25519, retries, Dockerfile, structured logging) | 🔜 |

## Repo

- **GitHub:** https://github.com/bantex01/P-Ork-Gateway
- **Related (P-Ork):** https://github.com/bantex01/P-Ork