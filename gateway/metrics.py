"""Prometheus metrics for the VectorStep Gateway.

Unlike VectorStep (which fetches from SQLite on each scrape), the Gateway is
in-memory so metrics are updated incrementally via standard prometheus_client
Counter/Gauge/Histogram objects.
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    REGISTRY,
)

_AGENT_DURATION_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, float("inf"))
_TOOL_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, float("inf"))
_ITERATION_BUCKETS = (1, 2, 3, 5, 8, 13, 20, float("inf"))

# ── Metric definitions ──────────────────────────────────────────────

_agent_runs_total = Counter(
    "vectorstep_gateway_agent_runs_total",
    "Total agent runs by agent, model, and terminal status",
    labelnames=["agent", "model", "status"],
)

_agent_runs_in_progress = Gauge(
    "vectorstep_gateway_agent_runs_in_progress",
    "Currently executing agent runs",
)

_agent_run_duration = Histogram(
    "vectorstep_gateway_agent_run_duration_seconds",
    "Agent run wall-clock duration in seconds",
    labelnames=["agent"],
    buckets=_AGENT_DURATION_BUCKETS,
)

_agent_iterations = Histogram(
    "vectorstep_gateway_agent_iterations",
    "Number of LLM iterations per agent run",
    labelnames=["agent"],
    buckets=_ITERATION_BUCKETS,
)

_agent_tool_calls = Counter(
    "vectorstep_gateway_agent_tool_calls_total",
    "Total tool calls made during agent runs",
    labelnames=["agent"],
)

_llm_tokens = Counter(
    "vectorstep_gateway_llm_tokens_total",
    "Total LLM tokens consumed",
    labelnames=["agent", "model", "direction"],
)

_tool_calls_total = Counter(
    "vectorstep_gateway_tool_calls_total",
    "Total MCP tool calls by server, tool, and result",
    labelnames=["mcp_server", "tool", "result"],
)

_tool_call_duration = Histogram(
    "vectorstep_gateway_tool_call_duration_seconds",
    "MCP tool call duration in seconds",
    labelnames=["mcp_server"],
    buckets=_TOOL_DURATION_BUCKETS,
)

_mcp_servers_running = Gauge(
    "vectorstep_gateway_mcp_servers_running",
    "1 if MCP server is running, 0 otherwise",
    labelnames=["mcp_server"],
)

_mcp_restarts = Counter(
    "vectorstep_gateway_mcp_restarts_total",
    "Total MCP server restarts",
    labelnames=["mcp_server"],
)

_sessions_active = Gauge(
    "vectorstep_gateway_sessions_active",
    "Number of active sessions in the SessionManager",
)

_build_info = Info(
    "vectorstep_gateway",
    "Gateway build information",
)


# ── Recording helpers ───────────────────────────────────────────────

def set_build_info(version: str = "unknown") -> None:
    _build_info.info({"version": version})


def record_agent_run_start() -> None:
    _agent_runs_in_progress.inc()


def record_agent_run_complete(
    agent: str,
    model: str,
    status: str,
    duration_s: float,
    iterations: int,
    tool_calls: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    _agent_runs_in_progress.dec()
    _agent_runs_total.labels(agent=agent, model=model, status=status).inc()
    _agent_run_duration.labels(agent=agent).observe(duration_s)
    _agent_iterations.labels(agent=agent).observe(iterations)
    _agent_tool_calls.labels(agent=agent).inc(tool_calls)
    _llm_tokens.labels(agent=agent, model=model, direction="input").inc(input_tokens)
    _llm_tokens.labels(agent=agent, model=model, direction="output").inc(output_tokens)


def record_tool_call(
    mcp_server: str,
    tool: str,
    result: str,
    duration_s: float,
) -> None:
    _tool_calls_total.labels(mcp_server=mcp_server, tool=tool, result=result).inc()
    _tool_call_duration.labels(mcp_server=mcp_server).observe(duration_s)


def record_mcp_restart(mcp_server: str) -> None:
    _mcp_restarts.labels(mcp_server=mcp_server).inc()


def update_mcp_server_status(mcp_server: str, running: bool) -> None:
    _mcp_servers_running.labels(mcp_server=mcp_server).set(1 if running else 0)


def update_session_count(count: int) -> None:
    _sessions_active.set(count)


def metrics_response():
    from fastapi.responses import Response
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
