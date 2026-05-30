# P-Ork Gateway

A lightweight Python/FastAPI WebSocket gateway that runs AI agents with MCP tool access. It acts as an executor backend for [P-Ork](https://github.com/bantex01/P-Ork) pipelines, providing an alternative to OpenClaw with support for multiple LLM providers and configurable MCP tool servers.

## Overview

The gateway sits between P-Ork and your LLM providers. P-Ork sends an agent request over WebSocket; the gateway runs the full agentic loop (LLM calls, MCP tool execution, multi-turn conversation) and returns the final result. P-Ork never sees intermediate tool calls or thinking content — it gets one clean response.

---

## Quick Start

```bash
# 1. Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Copy and edit the config template
cp templates/config.yaml.example config.yaml
# Edit config.yaml — set your LLM provider keys and MCP servers

# 3. Create your agents directory from the templates
cp -r templates/agents ./agents
# Edit agents/ to define your actual agents (model, tools, soul)

# 4. Set environment variables for any ${VAR_NAME} placeholders in config.yaml
export ANTHROPIC_API_KEY=sk-ant-...

# 5. Start the gateway
uvicorn gateway.main:app --port 18780

# 6. Find your operator token (auto-generated on first run)
cat ~/.pork-gateway/identity/device-auth.json
# Copy the 'operator' token — you'll need it for P-Ork's config
```

Both `config.yaml` and `agents/` are gitignored — they contain personal credentials and environment-specific agent definitions. Use the files in `templates/` as your starting point.

---

## Directory Structure

```
P-Ork-Gateway/
├── templates/                        # Starting point — copy these, don't edit in place
│   ├── config.yaml.example           # Config template with all options documented
│   └── agents/
│       ├── sre-triage/               # Example: SRE triage agent with tool access
│       │   ├── agent.yaml
│       │   └── soul.md
│       └── generic-pipeline-step/    # Example: minimal pipeline step agent
│           ├── agent.yaml
│           └── soul.md
├── agents/                           # Your agent definitions (gitignored)
├── config.yaml                       # Your config (gitignored)
├── gateway/
│   ├── main.py                       # FastAPI app, WebSocket endpoint, REST API
│   ├── auth/device.py                # Identity generation, token auth
│   ├── agents/loader.py              # Agent YAML + soul.md loader, SIGHUP reload
│   ├── session/manager.py            # Session key registry with prefix validation
│   ├── mcp/
│   │   ├── transport.py              # MCP stdio JSON-RPC subprocess transport
│   │   └── manager.py               # MCP process manager, tool registry, call routing
│   ├── llm/
│   │   ├── router.py                 # Model string → provider routing
│   │   ├── tool_translator.py        # MCP tool schemas ↔ provider format translation
│   │   └── providers/
│   │       ├── base.py               # BaseProvider ABC, ProviderResponse
│   │       ├── anthropic.py          # Anthropic API (native SDK, extended thinking)
│   │       ├── openrouter.py         # OpenRouter via httpx (OpenAI-compat)
│   │       ├── ollama.py             # Local Ollama (OpenAI-compat) + Ollama Cloud (native)
│   │       └── google.py             # Google Gemini via OpenAI-compat endpoint
│   ├── runner/agent_runner.py        # Full agentic loop: LLM ↔ MCP tool calls
│   └── models/
│       ├── config.py                 # GatewayConfig, ProviderConfig, LimitsConfig
│       └── agent.py                  # AgentConfig Pydantic model
└── requirements.txt
```

---

## Configuration (`config.yaml`)

Copy `templates/config.yaml.example` to `config.yaml` and edit. Values support `${VAR_NAME}` environment variable substitution throughout.

### `server`

| Field | Default | Description |
|---|---|---|
| `host` | `0.0.0.0` | Bind address |
| `port` | `18780` | Bind port. Use a different port from OpenClaw (18789) if running both. |

### `identity`

| Field | Default | Description |
|---|---|---|
| `path` | `~/.pork-gateway/identity` | Where identity files are stored. Auto-generated on first run. |

The operator token (used by P-Ork to authenticate) is written to `<path>/device-auth.json` on first run.

### `limits`

| Field | Default | Description |
|---|---|---|
| `max_agent_iterations` | `20` | Max LLM ↔ tool call loops before aborting a run |
| `request_timeout_seconds` | `180` | Timeout for a single LLM API call |
| `mcp_tool_timeout_seconds` | `30` | Timeout for a single MCP tool call |

### `mcp_servers`

Each key is a server name agents can reference in their `tools:` list. The gateway spawns each as a subprocess on startup using stdio JSON-RPC transport.

```yaml
mcp_servers:
  grafana:
    command: npx
    args: ["-y", "@grafana/mcp-grafana"]
    env:
      GRAFANA_URL: ${GRAFANA_URL}
      GRAFANA_TOKEN: ${GRAFANA_TOKEN}
```

| Field | Required | Description |
|---|---|---|
| `command` | Yes | Executable to run (`npx`, `python3`, etc.) |
| `args` | No | Arguments list |
| `env` | No | Environment variables for the subprocess. Supports `${VAR_NAME}` substitution. |

### `providers`

Each key becomes a model prefix for routing. Configure only the providers you need.

```yaml
providers:
  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
  openrouter:
    api_key: ${OPENROUTER_API_KEY}
    base_url: https://openrouter.ai/api/v1
  ollama-local:
    base_url: http://localhost:11434/v1
  ollama-cloud:
    api_key: ${OLLAMA_API_KEY}
    base_url: https://ollama.com/api
  google:
    api_key: ${GOOGLE_API_KEY}
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
```

| Field | Required | Description |
|---|---|---|
| `api_key` | No | API key. Empty string = no auth header sent. |
| `base_url` | No | Override the provider's default endpoint. |

**Default endpoints:**

| Provider key | Default `base_url` | Notes |
|---|---|---|
| `anthropic` | SDK default | Uses Anthropic Python SDK natively |
| `openrouter` | `https://openrouter.ai/api/v1` | OpenAI-compat |
| `ollama-local` | `http://localhost:11434/v1` | Local Ollama OpenAI-compat endpoint |
| `ollama-cloud` | `https://ollama.com/api` | Native Ollama `/api/chat` endpoint |
| `google` | `https://generativelanguage.googleapis.com/v1beta/openai` | OpenAI-compat |

---

## Model Routing

The prefix in a model string determines which provider handles the call:

| Model string | Provider | Notes |
|---|---|---|
| `anthropic/claude-sonnet-4-6` | Anthropic | Native SDK, extended thinking supported |
| `openrouter/deepseek/deepseek-chat` | OpenRouter | OpenAI-compat, thinking not supported |
| `ollama-local/qwen3:8b` | Local Ollama | OpenAI-compat via `/v1/chat/completions` |
| `ollama-cloud/gemma3:27b` | Ollama Cloud | Native Ollama `/api/chat` |
| `google/gemini-2.0-flash` | Google Gemini | OpenAI-compat |
| `claude-sonnet-4-6` | Anthropic | Bare name (no prefix) defaults to Anthropic |

The key name in `providers:` config must match the prefix in the model string exactly.

---

## Creating Agents

Agents live as subdirectories under `agents_dir` (default: `./agents/`). Each agent needs two files.

### `agent.yaml`

```yaml
name: sre-triage          # must match the directory name
model: anthropic/claude-sonnet-4-6
max_tokens: 8192
tools:                    # MCP server names from mcp_servers config
  - grafana
  - atlassian
```

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Agent identifier. Must match the directory name. Referenced as `agentId` in requests. |
| `model` | Yes | Default model string. Can be overridden per-request via `executor_config.model` in P-Ork. |
| `max_tokens` | Yes | Max output tokens per LLM call. |
| `tools` | No | MCP server names. The agent gets all tools from each listed server. Omit or leave empty for no tool access. |

### `soul.md`

The system prompt. Written in Markdown, sent as the `system` message to the LLM on every call.

Good soul files are:
- **Narrow in scope** — describe exactly what this agent does and does not do
- **Explicit about output format** — tell the model to return JSON only, no preamble
- **Clear on confidence scoring** — explain what high/low confidence means for this agent's task

See `templates/agents/` for annotated examples.

### Hot Reload

```bash
POST /reload          # via HTTP
kill -HUP <pid>       # via SIGHUP
```

Reloads all agent configs from disk without restarting. In-progress runs are unaffected.

---

## WebSocket Protocol

Connect to `ws://<host>:<port>/rpc`.

### Authentication

```json
// Gateway sends on connect:
{"type": "event", "event": "challenge", "payload": {"nonce": "abc123"}}

// Client sends connect request:
{"type": "req", "id": "uuid-1", "method": "connect", "params": {"auth": {"token": "your-operator-token"}}}

// Gateway responds:
{"type": "res", "id": "uuid-1", "ok": true, "payload": {"protocol": 3}}
```

### Agent Request

The gateway sends **two frames** with the same request `id`:

```json
// Client → Gateway
{
  "type": "req",
  "id": "uuid-2",
  "method": "agent",
  "params": {
    "agentId": "sre-triage",
    "sessionKey": "agent:sre-triage:pipeline:run-123:triage",
    "message": "Assess this alert: ...",
    "model": "anthropic/claude-opus-4-8",    // optional — overrides agent.yaml default
    "thinkingLevel": "medium"                // optional — Anthropic models only
  }
}

// Frame 1: accepted immediately (before agent runs)
{"type": "res", "id": "uuid-2", "ok": true, "payload": {"status": "accepted", "runId": "uuid-3"}}

// Frame 2: final result (after agent completes)
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
          "provider": "anthropic",
          "model": "claude-sonnet-4-6",
          "usage": {"input_tokens": 1234, "output_tokens": 456}
        },
        "aborted": false
      }
    }
  }
}
```

On error, frame 2 is: `{"type": "res", "id": "uuid-2", "ok": false, "error": {"message": "..."}}`

### Session Keys

Session keys must start with `agent:<agentId>:` — the gateway validates this to enforce isolation.

```
agent:sre-triage:pipeline:run-123:triage    ✅
pipeline:run-123:triage                     ❌ (missing agent prefix)
```

The P-Ork `gateway` executor generates a valid session key automatically if `session_key` is omitted from `executor_config`.

---

## REST Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/agents` | List loaded agents (name, model, tools) |
| `GET` | `/agents/{name}/soul` | Return the soul.md content for an agent |
| `POST` | `/reload` | Reload all agent configs from disk |
| `GET` | `/mcp/tools` | List all tools across all MCP servers |
| `GET` | `/mcp/servers` | List MCP server status (pid, tool count) |

---

## Environment Variables

`${VAR_NAME}` placeholders in `config.yaml` are resolved at startup. Commonly used:

| Variable | Used by | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | `providers.anthropic` | Anthropic API key |
| `OPENROUTER_API_KEY` | `providers.openrouter` | OpenRouter API key |
| `OLLAMA_API_KEY` | `providers.ollama-cloud` | Ollama Cloud API key — [get one here](https://ollama.com/settings/keys) |
| `GOOGLE_API_KEY` | `providers.google` | Google AI API key |
| `GRAFANA_URL` | `mcp_servers.grafana` | Grafana instance URL |
| `GRAFANA_TOKEN` | `mcp_servers.grafana` | Grafana service account token |
| `TAVILY_API_KEY` | `mcp_servers.tavily` | Tavily web search API key |
| `PORK_GATEWAY_CONFIG` | Gateway startup | Override config file path (default: `config.yaml`) |

---

## P-Ork Integration

### 1. Configure P-Ork

In P-Ork's `config.yaml`:

```yaml
executors:
  gateway:
    url: ws://localhost:18780/ws        # gateway WebSocket endpoint
    token: ${PORK_GATEWAY_TOKEN}        # operator token from device-auth.json
    rest_url: http://localhost:18780    # used by the P-Ork Agents UI
```

### 2. Use in pipeline YAML

```yaml
steps:
  - name: triage
    executor: gateway
    executor_config:
      agent: sre-triage                      # must match an agent in your agents/ directory
      model: anthropic/claude-sonnet-4-6     # optional model override
      thinking_level: low                    # optional — Anthropic models only
    confidence_threshold: 0.70
    on_low_confidence: escalate
    timeout_seconds: 300
    prompt_template: |
      Alert: {{summary}}
      Service: {{labels.service}}

      Investigate and return JSON...
```

Steps within the same P-Ork pipeline can freely mix `executor: openclaw` and `executor: gateway`.

### Differences from the OpenClaw executor

| | OpenClaw executor | Gateway executor |
|---|---|---|
| Auth | Ed25519 device signature | Bearer token |
| Session isolation | Server-side (no file clearing) | Server-side |
| Model routing | OpenClaw agent config | Gateway `providers:` config |
| MCP tools | OpenClaw MCP servers | Gateway `mcp_servers:` config |
| Thinking parameter | `thinking` | `thinkingLevel` |

---

## MCP Transport Notes

The gateway spawns each MCP server as a subprocess and communicates over stdio (JSON-RPC 2.0). The subprocess `stdout` stream is read with a 4MB line limit — sufficient for even large tool response payloads. If an MCP server fails to start, the gateway logs an error and continues; agents that list that server in their `tools:` will have no tools from it for that session.

MCP servers do not hot-reload — adding or removing a server requires a gateway restart.
