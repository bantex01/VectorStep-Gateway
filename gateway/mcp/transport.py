from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


class MCPTransport:
    """JSON-RPC 2.0 transport over a stdio subprocess."""

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str],
        tool_timeout: float = 30.0,
    ):
        self._command = command
        self._args = args
        self._env = {k: _resolve_env(v) for k, v in env.items()}
        self._tool_timeout = tool_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int | str, asyncio.Future] = {}
        self._next_id = 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        merged_env = {**os.environ, **self._env}
        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            limit=4 * 1024 * 1024,  # 4MB — MCP servers can return large tool-list responses
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="mcp-reader"
        )
        logger.debug("MCP subprocess started (pid=%s)", self._proc.pid)

    async def initialize(self) -> dict:
        resp = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pork-gateway", "version": "0.1.0"},
            },
            timeout=10.0,
        )
        await self.send_notification("notifications/initialized", {})
        return resp

    async def stop(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        logger.debug("MCP subprocess stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict]:
        resp = await self._request("tools/list", {}, timeout=10.0)
        return resp.get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> dict:
        resp = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self._tool_timeout,
        )
        return resp

    async def send_notification(self, method: str, params: dict) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._write(msg)

    async def send_request(self, method: str, params: dict) -> dict:
        return await self._request(method, params, timeout=self._tool_timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _alloc_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _request(self, method: str, params: dict, timeout: float) -> dict:
        rid = self._alloc_id()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut

        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        await self._write(msg)

        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"MCP request '{method}' timed out after {timeout}s")

        return result

    async def _write(self, obj: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("MCP subprocess not started")
        line = json.dumps(obj) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    logger.warning("MCP subprocess stdout closed")
                    break
                raw = line.decode().strip()
                if not raw:
                    continue
                try:
                    msg: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("MCP non-JSON line: %r", raw)
                    continue

                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if "error" in msg:
                        fut.set_exception(
                            RuntimeError(f"MCP error: {msg['error']}")
                        )
                    else:
                        fut.set_result(msg.get("result", {}))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("MCP reader error: %s", exc)
        finally:
            # Fail all outstanding futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("MCP transport closed"))
            self._pending.clear()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None
