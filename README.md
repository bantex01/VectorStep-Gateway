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
cp samples/config.yaml.example config.yaml
# Edit config.yaml — set your LLM provider keys and MCP servers

# 3. Create your agents directory
mkdir -p agents/my-agent
# Add agent.yaml and soul.md — see Creating Agents below

# 4. Set environment variables for any ${VAR_NAME} placeholders in config.yaml
export ANTHROPIC_API_KEY=sk-ant-...

# 5. Start the gateway
uvicorn gateway.main:app --port 18780

# 6. Find your operator token (auto-generated on first run)
cat ~/.pork-gateway/identity/device-auth.json
# Copy the 'operator' token — you'll need it for P-Ork's config
```

Both `config.yaml` and `agents/` are gitignored — they contain personal credentials and environment-specific agent definitions. Use `samples/config.yaml.example` as your starting point.

---

## Directory Structure

```
P-Ork-Gateway/
├── samples/
│   └── config.yaml.example           # Config template with all options documented
├── agents/                           # Your agent definitions (gitignored)
│   └── <agent-name>/
│       ├── agent.yaml
│       └── soul.md
├── config.yaml                       # Your config (gitignored)
├── gateway/
│   ├── main.py                       # FastAPI app, WebSocket endpoint, REST API
│   ├── tracing.py                    # OpenTelemetry setup and W3C trace context extraction
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
│       ├── config.py                 # GatewayConfig, ProviderConfig, LimitsConfig, OtelConfig
│       └── agent.py                  # AgentConfig Pydantic model
└── requirements.txt
```

---

## Configuration (`config.yaml`)

Copy `samples/config.yaml.example` to `config.yaml` and edit. Values support `${VAR_NAME}` environment variable substitution throughout.

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
| `llm_retry_attempts` | `2` | Retries on the *same* model after a retryable error (429/5xx/529/timeout/connection error) before falling over to the next entry in `model_fallbacks` |
| `llm_retry_base_delay_seconds` | `1.0` | Base delay for exponential backoff between retries (doubles each attempt: 1s, 2s, 4s, …) |

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

### `logging`

| Field | Default | Description |
|---|---|---|
| `level` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

### `observability`

Controls OpenTelemetry tracing. Disabled by default — all `tracer.start_as_current_span()` calls are no-ops until enabled.

```yaml
observability:
  otel:
    enabled: true
    exporter: otlp                                        # otlp | console
    endpoint: https://otlp-gateway-prod-eu-west-0.grafana.net/otlp/v1/traces
    service_name: pork-gateway
```

| Field | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable OTel tracing |
| `exporter` | `otlp` | `otlp` sends to an OTLP HTTP endpoint; `console` prints spans to stdout |
| `endpoint` | `http://localhost:4318/v1/traces` | OTLP HTTP endpoint. For Grafana Cloud, use your region's OTLP gateway URL with a Basic Auth header set via `OTEL_EXPORTER_OTLP_HEADERS`. |
| `service_name` | `pork-gateway` | `service.name` resource attribute on all spans |

When OTel is enabled, the gateway emits three span types per agent run:

| Span | Parent | Key attributes |
|---|---|---|
| `agent.run` | P-Ork `gen_ai.gateway` span (via W3C `traceparent`) | `agent.name`, `gen_ai.request.model`, `pork.gateway.iterations`, `pork.gateway.tool_calls`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| `llm_call` | `agent.run` | `llm_call.iteration`, `llm_call.attempt`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `llm_call.error`/`llm_call.retryable` (failed attempts only) |
| `tool_call <name>` | `agent.run` | `tool.name`, `tool.is_error` |

P-Ork injects a W3C `traceparent` header into the agent WebSocket request params, and the gateway extracts it to make `agent.run` a child of P-Ork's pipeline span — giving you a single unified trace across both services in Grafana Tempo.

**Grafana Cloud setup:**

1. Get your OTLP endpoint from Grafana Cloud portal → Connections → OpenTelemetry
2. Set `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(instanceId:apiKey)>` in your environment
3. Enable `observability.otel.enabled: true` and set `endpoint` to your Grafana Cloud OTLP URL

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
| `model_fallbacks` | No | List of model strings to try, in order, if `model` exhausts its retries (see `limits.llm_retry_attempts`). Once a fallback succeeds, later iterations in the same run try it first. |
| `max_tokens` | Yes | Max output tokens per LLM call. |
| `tools` | No | MCP server names, optionally scoped to specific tools (see below). Omit or leave empty for no tool access. |

```yaml
name: sre-triage
model: anthropic/claude-sonnet-4-6
model_fallbacks:
  - anthropic/claude-haiku-4-5
  - openrouter/deepseek/deepseek-chat
max_tokens: 8192
```

If `anthropic/claude-sonnet-4-6` returns a retryable error (e.g. `529 overloaded`), the gateway
retries it `llm_retry_attempts` times with exponential backoff, then falls over to
`claude-haiku-4-5`, then to the OpenRouter model if that also fails. Non-retryable errors (e.g.
`400`/`401`) skip the retry and fall over immediately.

#### Scoping `tools:` to specific tools

By default, listing an MCP server name in `tools:` grants every tool that server exposes. To
shrink the schema bloat in context (and the capability surface) for servers that expose dozens of
tools, scope an entry down to a `{server_name: [tool_a, tool_b]}` mapping instead of a bare name:

```yaml
tools:
  - filesystem                                    # every tool from filesystem
  - atlassian: [jira_search, jira_get_issue]       # only these two from atlassian
```

Tool names here are the unscoped MCP tool names (e.g. `jira_search`), not the namespaced
`server__tool` form used internally — check `GET /mcp/tools` for the exact names a server
exposes. Mixing scoped and unscoped entries in the same list is fine.

### `soul.md`

The system prompt. Written in Markdown, sent as the `system` message to the LLM on every call.

Good soul files are:
- **Narrow in scope** — describe exactly what this agent does and does not do
- **Explicit about output format** — tell the model to return JSON only, no preamble
- **Clear on confidence scoring** — explain what high/low confidence means for this agent's task

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

The gateway sends **multiple frames** with the same request `id`: one accepted frame immediately, zero or more streaming trace event frames during execution, then the final result frame.

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
    "thinkingLevel": "medium",               // optional — Anthropic models only
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"  // injected by P-Ork
  }
}

// Frame 1: accepted immediately (before agent runs)
{"type": "res", "id": "uuid-2", "ok": true, "payload": {"status": "accepted", "runId": "uuid-3"}}

// Frames 2..N: streaming trace events during execution (one per event)
{"type": "res", "id": "uuid-2", "ok": true, "payload": {"status": "trace_event", "event": {"type": "llm_call", "iteration": 1}}}
{"type": "res", "id": "uuid-2", "ok": true, "payload": {"status": "trace_event", "event": {"type": "tool_call", "name": "grafana_search", "input": {...}}}}
{"type": "res", "id": "uuid-2", "ok": true, "payload": {"status": "trace_event", "event": {"type": "tool_result", "name": "grafana_search", "content": "...", "is_error": false}}}

// Final frame: complete result
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
      },
      "trace": [
        {"type": "llm_call", "iteration": 1},
        {"type": "tool_call", "name": "grafana_search", "input": {...}},
        {"type": "tool_result", "name": "grafana_search", "content": "...", "is_error": false},
        {"type": "text", "content": "Agent response text here"}
      ]
    }
  }
}
```

On error, the final frame is: `{"type": "res", "id": "uuid-2", "ok": false, "error": {"message": "..."}}`

### Trace Event Types

| `type` | Fields | Description |
|---|---|---|
| `llm_call` | `iteration` | Start of an LLM call |
| `llm_retry` | `model`, `attempt`, `delay_seconds`, `error` | A retryable error occurred; retrying the same model after a backoff delay |
| `model_fallback` | `from_model`, `to_model`, `error` | Retries on `from_model` were exhausted; falling over to `to_model` |
| `thinking` | `content` | Extended thinking block (Anthropic only) |
| `text` | `content` | Text output block from the LLM |
| `tool_call` | `name`, `input` | Tool call about to be executed |
| `tool_result` | `name`, `content`, `is_error` | Result returned from MCP tool |

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
| `GET` | `/agents/{name}/agent` | Return the agent.yaml content for an agent |
| `POST` | `/reload` | Reload all agent configs from disk |
| `GET` | `/mcp/tools` | List all tools across all MCP servers |
| `GET` | `/mcp/servers` | List MCP server status (pid, tool count) |
| `GET` | `/metrics` | Prometheus metrics (no auth required) |

---

## Prometheus Metrics

The gateway exposes Prometheus-format metrics at `/metrics` (GET). No authentication is required — Prometheus scrapers connect directly.

### Example Prometheus scrape config

```yaml
scrape_configs:
  - job_name: pork-gateway
    static_configs:
      - targets: ["localhost:18780"]
```

### Exposed metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `pork_gateway_agent_runs_total` | Counter | `agent`, `model`, `status` | Total agent runs by agent, model, and terminal status (`ok`/`error`/`timeout`/`max_iterations`) |
| `pork_gateway_agent_runs_in_progress` | Gauge | — | Currently executing agent runs |
| `pork_gateway_agent_run_duration_seconds` | Histogram | `agent` | Agent run wall-clock duration in seconds |
| `pork_gateway_agent_iterations` | Histogram | `agent` | Number of LLM iterations per agent run |
| `pork_gateway_agent_tool_calls_total` | Counter | `agent` | Total tool calls made during agent runs |
| `pork_gateway_llm_tokens_total` | Counter | `agent`, `model`, `direction` | Total LLM tokens consumed (`direction`: `input`/`output`) |
| `pork_gateway_tool_calls_total` | Counter | `mcp_server`, `tool`, `result` | Total MCP tool calls by server, tool, and result (`success`/`error`/`timeout`) |
| `pork_gateway_tool_call_duration_seconds` | Histogram | `mcp_server` | MCP tool call duration in seconds |
| `pork_gateway_mcp_servers_running` | Gauge | `mcp_server` | 1 if MCP server is running, 0 otherwise |
| `pork_gateway_mcp_restarts_total` | Counter | `mcp_server` | Total MCP server restarts |
| `pork_gateway_sessions_active` | Gauge | — | Number of active sessions |
| `pork_gateway_info` | Info | `version` | Build information |

### Example PromQL queries

```promql
# Agent run success rate (last 5 minutes)
rate(pork_gateway_agent_runs_total{status="ok"}[5m])
  / rate(pork_gateway_agent_runs_total[5m])

# Average agent run duration by agent
rate(pork_gateway_agent_run_duration_seconds_sum[5m])
  / rate(pork_gateway_agent_run_duration_seconds_count[5m])

# MCP tool error rate by server
rate(pork_gateway_tool_calls_total{result="error"}[5m])
  / rate(pork_gateway_tool_calls_total[5m])

# Currently running agents
pork_gateway_agent_runs_in_progress

# Active sessions
pork_gateway_sessions_active
```

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
| `OTEL_EXPORTER_OTLP_HEADERS` | OTel exporter | Auth headers for OTLP endpoint (e.g. Grafana Cloud Basic Auth) |

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
| OTel trace propagation | Not supported | Supported — joins P-Ork's trace |

---

## Performance Notes

- **Anthropic prompt caching** — the `soul` system prompt and tool-schema list are sent with
  `cache_control: {"type": "ephemeral"}` (`gateway/llm/providers/anthropic.py`), so on multi-turn
  loops the unchanged prefix is served from Anthropic's cache instead of being re-billed as full
  input tokens on every iteration. Anthropic-only — OpenAI-compat providers don't expose this.
- **Parallel tool execution** — when an LLM turn requests multiple tools at once, the gateway
  runs them concurrently with `asyncio.gather` instead of one at a time
  (`gateway/runner/agent_runner.py`), so the turn waits for the slowest tool call rather than the
  sum of all of them.
- **Model fallback chains + retry with backoff** — a retryable error (429/5xx/529/timeout/
  connection error) is retried on the same model with exponential backoff
  (`limits.llm_retry_attempts`/`llm_retry_base_delay_seconds`); once exhausted, the gateway falls
  over to the next model in the agent's `model_fallbacks` list. Non-retryable errors (e.g.
  `400`/`401`) skip straight to fallover. See `llm_retry`/`model_fallback` trace events above.

---

## MCP Transport Notes

The gateway spawns each MCP server as a subprocess and communicates over stdio (JSON-RPC 2.0). The subprocess `stdout` stream is read with a 4MB line limit — sufficient for even large tool response payloads. If an MCP server fails to start, the gateway logs an error and continues; agents that list that server in their `tools:` will have no tools from it for that session.

MCP servers do not hot-reload — adding or removing a server requires a gateway restart.
