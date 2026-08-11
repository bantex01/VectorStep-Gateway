# VectorStep Gateway

A lightweight Python/FastAPI WebSocket gateway that runs AI agents with MCP tool access, used as an executor backend for VectorStep pipelines.

## What it is

The gateway sits between VectorStep and your LLM providers. VectorStep sends an agent request over WebSocket; the gateway runs the full agentic loop (LLM calls, MCP tool execution, multi-turn conversation) and returns the final result. VectorStep never sees intermediate tool calls or thinking content — it gets one clean response.

It supports multiple LLM providers (Anthropic, OpenRouter, Google, Azure OpenAI, Ollama) and configurable MCP tool servers behind a single agent config, as an alternative to routing through OpenClaw.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp samples/config.yaml.example config.yaml
# Edit config.yaml — set your LLM provider keys and MCP servers

mkdir -p agents/my-agent
# Add agent.yaml and soul.md

export ANTHROPIC_API_KEY=sk-ant-...
python -m gateway.main

# Verify: operator token generated on first run
cat ~/.vectorstep-gateway/identity/device-auth.json
```

Both `config.yaml` and `agents/` are gitignored — they hold credentials and environment-specific agent definitions. Full walkthrough, including wiring the gateway up to VectorStep: [Quick start](https://vectorstep.io/docs/getting-started/quick-start/). Full reference: [Installation](https://vectorstep.io/docs/getting-started/installation/).

## Documentation

Full docs at [vectorstep.io](https://vectorstep.io/docs/):

| Section | Covers |
|---|---|
| [Gateway](https://vectorstep.io/docs/gateway/overview/) | Configuration, providers, agent authoring, WebSocket protocol, REST API, operations |
| [Sources & Executors](https://vectorstep.io/docs/integrations/mcp/) | MCP servers, the `gateway` executor from VectorStep's side |
| [Concepts](https://vectorstep.io/docs/concepts/architecture/) | Architecture, confidence & the trust vector |
| [Design & Internals](https://vectorstep.io/docs/design/decisions/) | Design decisions, extending VectorStep |

## The ecosystem

| Repo | Role |
|---|---|
| **VectorStep** | The orchestration service: webhook intake, pipeline runner, trust gating, UI, analytics |
| **VectorStep-Gateway** | WebSocket gateway that runs agents: LLM providers, MCP tools, the full agentic loop |
| **VectorStep-Service-MCP** | MCP server exposing pipeline authoring, run inspection and analytics to Claude Code/Desktop |
| **VectorStep-Gateway-MCP** | MCP server for authoring and inspecting Gateway agents |

## Licence and contributions

Apache-2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Contributions are welcome: see [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and [Licence & contributions](https://vectorstep.io/docs/about/licence-and-contributions/).
