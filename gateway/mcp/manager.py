from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel

from gateway.mcp.transport import MCPTransport
from gateway.models.agent import AgentConfig
from gateway.models.config import GatewayConfig, MCPServerConfig

logger = logging.getLogger(__name__)


class MCPTool(BaseModel):
    name: str
    description: str
    input_schema: dict        # JSON Schema from MCP inputSchema
    server_name: str          # which MCP server owns this tool


class MCPToolResult(BaseModel):
    content: list[dict]       # MCP content blocks
    is_error: bool = False


class _ServerState:
    def __init__(self, name: str, cfg: MCPServerConfig, tool_timeout: float):
        self.name = name
        self.cfg = cfg
        self.tool_timeout = tool_timeout
        self.transport: MCPTransport | None = None
        self.restart_count: int = 0
        self._monitor_task: asyncio.Task | None = None

    def _make_transport(self) -> MCPTransport:
        return MCPTransport(
            command=self.cfg.command,
            args=self.cfg.args,
            env=self.cfg.env,
            tool_timeout=self.tool_timeout,
        )

    @property
    def running(self) -> bool:
        if self.transport is None:
            return False
        rc = self.transport.returncode
        return rc is None  # None means still running

    @property
    def pid(self) -> int | None:
        return self.transport.pid if self.transport else None


class MCPManager:
    def __init__(self, config: GatewayConfig):
        self._config = config
        self._tool_timeout = float(config.limits.mcp_tool_timeout_seconds)
        self._servers: dict[str, _ServerState] = {
            name: _ServerState(name, cfg, self._tool_timeout)
            for name, cfg in config.mcp_servers.items()
        }
        # Global tool registry: tool_name -> MCPTool
        self._tools: dict[str, MCPTool] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        for state in self._servers.values():
            await self._spawn_and_init(state)
            state._monitor_task = asyncio.create_task(
                self._monitor(state), name=f"mcp-monitor-{state.name}"
            )

    async def stop_all(self) -> None:
        for state in self._servers.values():
            if state._monitor_task and not state._monitor_task.done():
                state._monitor_task.cancel()
                try:
                    await state._monitor_task
                except asyncio.CancelledError:
                    pass
            if state.transport:
                try:
                    await state.transport.stop()
                except Exception as exc:
                    logger.warning("Error stopping MCP server %s: %s", state.name, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tools_for_agent(self, agent: AgentConfig) -> list[MCPTool]:
        if not agent.tools:
            return []
        return [
            self._tools[name]
            for name in agent.tools
            if name in self._tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise KeyError(f"Unknown tool: {tool_name}")

        state = self._servers.get(tool.server_name)
        if state is None or not state.running or state.transport is None:
            raise RuntimeError(f"MCP server '{tool.server_name}' is not running")

        raw = await state.transport.call_tool(tool_name, arguments)
        return MCPToolResult(
            content=raw.get("content", []),
            is_error=raw.get("isError", False),
        )

    def get_server_status(self) -> dict:
        return {
            name: {
                "running": state.running,
                "pid": state.pid,
                "restart_count": state.restart_count,
            }
            for name, state in self._servers.items()
        }

    def get_all_tools(self) -> dict[str, list[dict]]:
        """Return tools grouped by server, formatted for the HTTP endpoint."""
        grouped: dict[str, list[dict]] = {name: [] for name in self._servers}
        for tool in self._tools.values():
            grouped.setdefault(tool.server_name, []).append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
            )
        return grouped

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _spawn_and_init(self, state: _ServerState) -> bool:
        """Spawn subprocess, run MCP initialize handshake, cache tools. Returns success."""
        try:
            transport = state._make_transport()
            await transport.start()
            state.transport = transport

            await transport.initialize()
            logger.info("MCP server '%s' initialized (pid=%s)", state.name, transport.pid)

            await self._cache_tools(state)
            return True
        except Exception as exc:
            logger.error("Failed to start MCP server '%s': %s", state.name, exc)
            if state.transport:
                try:
                    await state.transport.stop()
                except Exception:
                    pass
                state.transport = None
            return False

    async def _cache_tools(self, state: _ServerState) -> None:
        assert state.transport is not None
        raw_tools = await state.transport.list_tools()

        registered = 0
        for t in raw_tools:
            name = t["name"]
            if name in self._tools:
                logger.warning(
                    "Tool name collision: '%s' already registered from server '%s'; "
                    "overwriting with server '%s'",
                    name,
                    self._tools[name].server_name,
                    state.name,
                )
            self._tools[name] = MCPTool(
                name=name,
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=state.name,
            )
            registered += 1

        logger.info(
            "MCP server '%s': cached %d tool(s)", state.name, registered
        )

    async def _monitor(self, state: _ServerState) -> None:
        """Watch the subprocess and restart it if it exits unexpectedly."""
        try:
            while True:
                await asyncio.sleep(2.0)
                if state.transport is None:
                    continue
                if not state.running:
                    rc = state.transport.returncode
                    logger.warning(
                        "MCP server '%s' exited (rc=%s); restarting…",
                        state.name,
                        rc,
                    )
                    # Remove this server's tools before restart
                    self._tools = {
                        k: v
                        for k, v in self._tools.items()
                        if v.server_name != state.name
                    }
                    state.restart_count += 1
                    ok = await self._spawn_and_init(state)
                    if ok:
                        logger.info(
                            "MCP server '%s' restarted successfully (restart #%d)",
                            state.name,
                            state.restart_count,
                        )
                    else:
                        logger.error(
                            "MCP server '%s' failed to restart; will retry",
                            state.name,
                        )
        except asyncio.CancelledError:
            pass
