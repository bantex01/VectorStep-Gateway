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

# 5. Start the gateway (host/port come from config.yaml's `server:` section)
python -m gateway.main

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
│   │       ├── google.py             # Google Gemini via OpenAI-compat endpoint
│   │       ├── azure.py             # Azure OpenAI (OpenAI-compat, api-key header auth)
│   │       └── yolo.py              # Generic OpenAI-compat custom endpoint
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
| `max_concurrent_runs` | `10` | Gateway-wide cap on simultaneously executing agent runs. Once at capacity, new requests are still accepted (`Frame 1`) but queue until a slot frees up. |
| `trace_tool_result_max_chars` | `3000` | Gateway-wide default cap on a `tool_result` trace event's `content`, truncated with a trailing `"… [truncated]"` marker. This **only** affects the trace copy — the streamed/persisted record a caller (or a downstream grounding judge) inspects. The LLM conversation itself always receives the tool's full, untruncated output regardless of this setting; nothing about the agent's own reasoning is affected by it. A caller can override this per-request via the agent request's `traceToolResultMax` (see below) without changing the gateway-wide default. |

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
  azure:
    api_key: ${AZURE_OPENAI_API_KEY}
    resource_name: ${AZURE_OPENAI_RESOURCE}   # e.g. "my-company-openai"
    api_version: "2025-01-01-preview"         # optional, this is the default
  yolo:
    api_key: ${YOLO_API_KEY}
    base_url: https://your-provider.example.com/v1
```

Most providers support `api_key` and `base_url`. Azure uses different fields:

| Field | Required | Description |
|---|---|---|
| `api_key` | No | API key. Empty string = no auth header sent. |
| `base_url` | No | Override the provider's default endpoint (not used for `azure`). |

**Azure-specific fields** (under `providers.azure`):

| Field | Required | Description |
|---|---|---|
| `api_key` | Yes | Azure OpenAI API key from Azure AI Foundry. |
| `resource_name` | Yes | Azure resource name — the subdomain part of `{resource_name}.openai.azure.com`. |
| `api_version` | No | Azure API version. Default: `2025-01-01-preview`. |

**Default endpoints:**

| Provider key | Default endpoint | Notes |
|---|---|---|
| `anthropic` | SDK default | Uses Anthropic Python SDK natively |
| `openrouter` | `https://openrouter.ai/api/v1` | OpenAI-compat |
| `ollama-local` | `http://localhost:11434/v1` | Local Ollama OpenAI-compat endpoint |
| `ollama-cloud` | `https://ollama.com/api` | Native Ollama `/api/chat` endpoint |
| `google` | `https://generativelanguage.googleapis.com/v1beta/openai` | OpenAI-compat |
| `azure` | `https://{resource_name}.openai.azure.com/openai/deployments/{deployment}/chat/completions` | OpenAI-compat, auth via `api-key` header |
| `yolo` | None — `base_url` required | Generic OpenAI-compat custom endpoint, e.g. a self-hosted or third-party API |

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
| `azure/gpt-4o` | Azure OpenAI | OpenAI-compat; `gpt-4o` is the deployment name |
| `yolo/some-model` | Yolo (custom endpoint) | OpenAI-compat, `base_url` from `providers.yolo` |
| `claude-sonnet-4-6` | Anthropic | Bare name (no prefix) defaults to Anthropic |

### Azure OpenAI

For Azure, the model string suffix is the **deployment name** you set up in Azure AI Foundry (not the underlying model family name). If you deployed GPT-4o and named the deployment `gpt-4o`, the model string is `azure/gpt-4o`. Different deployments of the same underlying model can have different names.

```yaml
# agent.yaml
name: my-azure-agent
model: azure/gpt-4o           # deployment name from Azure AI Foundry
max_tokens: 4096
model_fallbacks:
  - azure/gpt-4o-mini         # cheaper fallback deployment
  - anthropic/claude-haiku-4-5-20251001  # cross-provider fallback
```

Azure's API is OpenAI-compatible. The differences handled internally are the endpoint URL format, the `api-key` request header (instead of `Authorization: Bearer`), and the `max_completion_tokens` parameter (Azure's chat completions API, like OpenAI's, rejects `max_tokens` for reasoning-family deployments — the provider sends `max_completion_tokens` on the wire regardless of deployment, translated transparently from the agent's `max_tokens` field). Extended thinking is not available on Azure OpenAI.

The key name in `providers:` config must match the prefix in the model string exactly.

**Reasoning-family deployments (gpt-5, o1, o3) and token budgets:** these models spend part of their `max_tokens` budget on hidden internal reasoning before producing any visible output. A budget that's fine for `gpt-4o` (e.g. `max_tokens: 200`) can come back with `stop_reason: length` and zero visible text on `gpt-5` because reasoning consumed the whole budget. Set `max_tokens` generously (2000+) for reasoning-family deployments to leave headroom for actual output.

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

Editing `agent.yaml` or `soul.md` changes the agent's `version` (a content hash over the
agent's entire config, exposed as `agentMeta.agentVersion` and on the `GET /agents` /
`GET /agents/{name}` endpoints) and therefore resets that agent's calibration history in
P-Ork — see P-Ork's `CONFIDENCE-EXPLAINED.md`.

### Startup Config Validation

When agents are loaded (at startup and on every `POST /reload` / SIGHUP), the gateway validates each agent's `model` and `model_fallbacks` against the configured providers:

- **Unrecognized prefix** (e.g. `my-custom/model`) — logged as `ERROR`. The agent will load but every request will fail with a `KeyError` at runtime.
- **Known prefix, missing api_key** (e.g. `openrouter/...` but `providers.openrouter.api_key` is empty) — logged as `WARNING`. The agent will load but requests will fail with auth errors.

Local Ollama (`ollama/...`) is exempt from the api_key check — it requires no credentials by default.

These are warnings/errors in the log, not hard failures. All other agents continue to load normally. Check startup logs if an agent behaves unexpectedly at request time.

### Hot Reload

```bash
POST /reload          # via HTTP
kill -HUP <pid>       # via SIGHUP
```

Reloads all agent configs from disk without restarting. In-progress runs are unaffected. Validation runs against the reloaded agents on every reload.

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
    "traceToolResultMax": 8000,               // optional — overrides limits.trace_tool_result_max_chars for this request only
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
          "agentVersion": "91f02ab3c7de",
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

`agentMeta.provider` is the provider key (`anthropic`, `openrouter`, `azure`, etc.) that actually served the request — if `model_fallbacks` kicked in and a later candidate from a *different* provider ended up serving it, `provider` reflects that final candidate, not the originally requested model. This is deliberately distinct from `agentMeta.model`, which is whatever the underlying LLM API itself reports as the model name — for most OpenAI-compat providers that's the raw vendor model id (e.g. OpenRouter reports `"deepseek/deepseek-v4-pro-..."`, not `"openrouter/deepseek/..."`), so `model` alone can't be used to reliably reconstruct which provider served a call.

`agentMeta.agentVersion` is a content hash of the agent's full config, including `soul.md` (see [Creating Agents](#creating-agents)) — it changes whenever `agent.yaml` or `soul.md` changes. P-Ork uses it to scope calibration buckets, so two runs under different `agentVersion`s are never pooled as evidence for the same track record.

### Trace Event Types

| `type` | Fields | Description |
|---|---|---|
| `llm_call` | `iteration` | Start of an LLM call |
| `llm_retry` | `model`, `attempt`, `delay_seconds`, `error` | A retryable error occurred; retrying the same model after a backoff delay |
| `model_fallback` | `from_model`, `to_model`, `error` | Retries on `from_model` were exhausted; falling over to `to_model` |
| `thinking` | `content` | Extended thinking block (Anthropic only) |
| `text` | `content` | Text output block from the LLM |
| `tool_call` | `name`, `input` | Tool call about to be executed |
| `tool_result` | `name`, `content`, `is_error` | Result returned from MCP tool. `content` is capped at `limits.trace_tool_result_max_chars` (default 3000, overridable per-request via `traceToolResultMax` — see `limits` above) — this is a trace-only truncation; the LLM's own conversation always sees the full result. |

### Session Keys

Session keys must start with `agent:<agentId>:` — the gateway validates this prefix on every request.

```
agent:sre-triage:pipeline:run-123:triage    ✅
pipeline:run-123:triage                     ❌ (missing agent prefix)
```

The P-Ork `gateway` executor generates a valid session key automatically if `session_key` is omitted from `executor_config`.

**What session keys do and don't do:** the gateway tracks session keys in memory (used for the `pork_gateway_sessions_active` metric) but does **not** persist message history between requests. Every agent call starts with a fresh message list containing only the current prompt. Session keys in this gateway provide namespace isolation and prefix validation — not conversational continuity across calls.

This is intentional for P-Ork's usage pattern: session keys are scoped per pipeline run and step (e.g. `agent:sre-triage:pipeline:{{pipeline_run_id}}:triage`), so no two invocations of the same step share a key. Context passing between steps is handled explicitly by P-Ork via `next_step_context`, prompt templates, and `{{loop.prior_output}}` — the pipeline author controls exactly what each step sees, rather than the agent accumulating unbounded conversation history.

### Concurrency and Cancellation

Each `agent` request is gated by a gateway-wide semaphore sized by `limits.max_concurrent_runs`
(default `10`). The `accepted` frame (with `runId`) is always sent immediately; if the gateway is
already at capacity, the run queues silently behind it — no trace events fire until a slot frees
up and the run actually starts.

If the client disconnects (the WebSocket closes) while a run is in flight — whether still queued
or already executing — the gateway cancels it immediately rather than letting the agentic loop run
to completion for a response nobody will receive. Cancelled runs are recorded with
`status="aborted"` in the `pork_gateway_agent_runs_total` metric.

---

## REST Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health — status, agent count, MCP server states, active run count |
| `GET` | `/agents` | List loaded agents (name, model, model_fallbacks, tools, version) |
| `GET` | `/agents/{name}` | Combined structured view of one agent — parsed config + soul.md + raw agent.yaml text + version |
| `GET` | `/agents/{name}/soul` | Return the soul.md content for an agent |
| `GET` | `/agents/{name}/agent` | Return the agent.yaml content for an agent |
| `POST` | `/agents` | Create a new agent from raw `agent.yaml`/`soul.md` text — validates, writes, reloads |
| `PUT` | `/agents/{name}` | Update an existing agent (either or both files) — validates, writes, reloads |
| `DELETE` | `/agents/{name}` | Delete an agent — returns its prior `agent.yaml`/`soul.md` content for audit |
| `POST` | `/agents/validate` | Dry-run validation of a candidate agent — no write |
| `GET` | `/providers` | Configured providers + their model-string prefix (no API keys, no live model list) |
| `POST` | `/reload` | Reload all agent configs from disk |
| `GET` | `/mcp/tools` | List all tools across all MCP servers |
| `GET` | `/mcp/servers` | List MCP server status (pid, tool count) |
| `GET` | `/metrics` | Prometheus metrics (no auth required) |

### `/health` response

```json
{
  "status": "ok",
  "version": "0.5.0",
  "agents": 3,
  "active_runs": 1,
  "max_concurrent_runs": 10,
  "mcp_servers": {
    "grafana": {"running": true, "restart_count": 0},
    "atlassian": {"running": true, "restart_count": 1}
  }
}
```

`status` is `"ok"` when all configured MCP servers are running, `"degraded"` if any are down. A gateway with no MCP servers configured always returns `"ok"`. No authentication is required — suitable for Kubernetes liveness/readiness probes.

### Agent management endpoints

`POST /agents`, `PUT /agents/{name}`, and `DELETE /agents/{name}` are the write path behind the [Gateway MCP](#gateway-mcp-agent-authoring)'s `create_agent`/`update_agent`/`delete_agent` tools — an `agent.yaml`/`soul.md` pair is validated (schema **and** that `model`/`model_fallbacks` map to a configured provider and `tools:` map to configured `mcp_servers`), atomically written, and the live registry reloaded, all before the request returns. A candidate that fails validation never touches disk — see `gateway/agent_writer.py`.

`POST /agents` request body:

```json
{
  "name": "sre-triage",
  "agent_yaml": "name: sre-triage\nmodel: anthropic/claude-sonnet-4-6\ntools: [grafana]\n",
  "soul_md": "You are an SRE triage agent...",
  "overwrite": false
}
```

Success response (200):

```json
{
  "agent": {"name": "sre-triage", "agent_yaml": "...", "soul_md": "..."},
  "committed": false,
  "note": "Files written and reloaded. agents/ is gitignored, so this is not a git-commit concern."
}
```

`agents/` is gitignored (personal to the deployment, unlike P-Ork's git-controlled `pipelines/`), so `committed` is always `false` — there is nothing to commit.

`PUT /agents/{name}` accepts `agent_yaml` and/or `soul_md` — omit one to leave that file untouched. The YAML's own `name:` field must always match the `name` used to create it (in the POST body) or the URL `{name}` (for PUT) — a rename is a delete + create, not an update.

Error responses carry an explicit `type` so a caller never has to infer it from status code + message wording:

```json
// 400 — e.g. tools: references an unconfigured MCP server, or model maps to no known provider
{"detail": {"type": "validation", "message": "...", "errors": [{"agent": "...", "field": "tools", "value": "...", "message": "...", "severity": "error"}]}}

// 404 — PUT/DELETE on an agent that doesn't exist
{"detail": {"type": "not_found", "message": "Agent 'x' not found"}}

// 409 — POST on an existing name without overwrite: true
{"detail": {"type": "collision", "message": "Agent 'x' already exists"}}
```

`POST /agents/validate` (body: `{"agent_yaml": "...", "soul_md": "..."}`, `soul_md` optional) runs the same checks with no write — returns `{"valid": bool, "errors": [...]}`. This is the safe iterate loop before calling `POST`/`PUT /agents`.

`GET /providers` returns provider names, whether each has credentials configured, and the model-string prefix to use (e.g. `"openrouter/"`) — never API keys, and no live per-provider model enumeration:

```json
{"providers": [
  {"name": "anthropic", "configured": true, "prefix": null},
  {"name": "openrouter", "configured": false, "prefix": "openrouter/"}
]}
```

(`prefix: null` for Anthropic — a bare model name with no prefix routes there by default, per [Model Routing](#model-routing).)

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
| `pork_gateway_agent_runs_total` | Counter | `agent`, `model`, `status` | Total agent runs by agent, model, and terminal status (`ok`/`error`/`timeout`/`max_iterations`/`aborted`) |
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
| `AZURE_OPENAI_API_KEY` | `providers.azure.api_key` | Azure OpenAI API key |
| `AZURE_OPENAI_RESOURCE` | `providers.azure.resource_name` | Azure resource name (subdomain of `.openai.azure.com`) |
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

## Gateway MCP (agent authoring)

[`P-Ork-Gateway-MCP`](../P-Ork-Gateway-MCP) is a separate, standalone MCP server (own repo, own process) that exposes this gateway's agent-management and introspection surface — `list_agents`/`get_agent`/`list_mcp_servers`/`list_mcp_tools`/`list_providers`/`get_metrics`/`validate_agent` for reads, `create_agent`/`update_agent`/`delete_agent`/`reload` for writes — to an MCP client (Claude Code/Desktop), so an agent's `agent.yaml`/`soul.md` can be authored conversationally instead of by hand-editing files on the host running the gateway.

It talks to this gateway only over the REST endpoints above (the `/agents`/`/providers` family) — no shared code, no imports of `gateway.*`. It holds the **operator token** (`GATEWAY_OPERATOR_TOKEN`, the value in `<identity>/device-auth.json`) and never returns it, a provider API key, or any other `config.yaml` secret from any tool. See that repo's README for setup.

This is the agent-authoring counterpart to [P-Ork's own MCP server](../P-Ork-Service-MCP), which handles pipelines/steps — the two have non-overlapping tool sets (agents live here; pipelines live in P-Ork).

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
