import asyncio
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from gateway.agent_writer import WriteResult, delete_agent as delete_agent_files, validate_agent as validate_agent_files, write_agent
from gateway.agents.loader import (
    install_sighup_handler,
    load_agents,
    provider_configured_map,
    validate_agent_models,
)
from gateway.auth.device import bootstrap_identity, get_operator_token
from gateway.llm.router import PREFIX_TO_PROVIDER, LLMRouter
from gateway.mcp.manager import MCPManager
from gateway.models.agent import AgentConfig
from gateway.models.config import GatewayConfig, load_config
from gateway.runner.agent_runner import AgentRunError, AgentRunner
from gateway.session.manager import SessionManager
from gateway.metrics import metrics_response, set_build_info, update_mcp_server_status
from gateway.tracing import extract_remote_context, setup_tracing, shutdown_tracing

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = os.environ.get("PORK_GATEWAY_CONFIG", "config.yaml")
    config: GatewayConfig = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    setup_tracing(config)

    auth_data = bootstrap_identity(config.identity)
    operator_token = get_operator_token(auth_data)

    agents = load_agents(config.agents_dir)
    validate_agent_models(agents, config)
    session_manager = SessionManager()

    mcp_manager = MCPManager(config)
    await mcp_manager.start_all()
    set_build_info(version="0.5.0")
    for name, status in mcp_manager.get_server_status().items():
        update_mcp_server_status(name, status["running"])

    llm_router = LLMRouter(config)
    agent_runner = AgentRunner(llm_router)

    _state["config"] = config
    _state["operator_token"] = operator_token
    _state["agents"] = agents
    _state["session_manager"] = session_manager
    _state["mcp_manager"] = mcp_manager
    _state["agent_runner"] = agent_runner
    # Gates total concurrent agent runs gateway-wide — acquiring blocks (queues)
    # rather than rejecting once the gateway is at capacity.
    _state["run_semaphore"] = asyncio.Semaphore(config.limits.max_concurrent_runs)

    def _reload():
        new_agents = load_agents(config.agents_dir)
        validate_agent_models(new_agents, config)
        _state["agents"] = new_agents
        logging.getLogger(__name__).info(
            "Reloaded %d agent(s): %s", len(new_agents), list(new_agents)
        )

    _state["reload_fn"] = _reload
    install_sighup_handler(_reload)

    logging.getLogger(__name__).info(
        "Gateway ready — %d agent(s) loaded: %s",
        len(agents),
        list(agents),
    )

    yield

    await mcp_manager.stop_all()
    await llm_router.aclose()
    shutdown_tracing()


app = FastAPI(lifespan=lifespan)


@app.get("/metrics")
async def get_metrics():
    return metrics_response()


@app.get("/agents")
async def list_agents():
    agents: dict[str, AgentConfig] = _state.get("agents", {})
    return {
        "agents": [
            {"name": a.name, "model": a.model, "model_fallbacks": a.model_fallbacks, "tools": a.tools}
            for a in agents.values()
        ]
    }


@app.get("/agents/{agent_name}/soul")
async def get_agent_soul(agent_name: str):
    agents: dict[str, AgentConfig] = _state.get("agents", {})
    agent = agents.get(agent_name)
    if not agent:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_name}' not found"})
    return {"name": agent.name, "content": agent.soul}


@app.get("/agents/{agent_name}/agent")
async def get_agent_config_file(agent_name: str):
    agents: dict[str, AgentConfig] = _state.get("agents", {})
    if agent_name not in agents:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_name}' not found"})
    config: GatewayConfig = _state["config"]
    yaml_path = Path(config.agents_dir) / agent_name / "agent.yaml"
    if not yaml_path.exists():
        return JSONResponse(status_code=404, content={"error": "agent.yaml not found"})
    return {"name": agent_name, "content": yaml_path.read_text()}


@app.get("/agents/{agent_name}")
async def get_agent(agent_name: str):
    """Combined structured view of one agent — parsed config + soul.md text +
    the raw agent.yaml text (for round-trip editing), in one call instead of
    the two separate raw endpoints above."""
    agents: dict[str, AgentConfig] = _state.get("agents", {})
    agent = agents.get(agent_name)
    if not agent:
        return JSONResponse(status_code=404, content={"error": f"Agent '{agent_name}' not found"})
    config: GatewayConfig = _state["config"]
    yaml_path = Path(config.agents_dir) / agent_name / "agent.yaml"
    return {
        "name": agent.name,
        "config": agent.model_dump(exclude={"soul"}),
        "agent_yaml": yaml_path.read_text() if yaml_path.exists() else "",
        "soul_md": agent.soul,
    }


@app.get("/providers")
async def list_providers():
    """Configured providers + their model-string prefix — no keys, no live
    model enumeration (a caller composes model strings as '<prefix><model>',
    or bare for anthropic)."""
    config: GatewayConfig = _state["config"]
    configured = provider_configured_map(config)
    prefix_by_provider = {v: k for k, v in PREFIX_TO_PROVIDER.items()}
    return {
        "providers": [
            {
                "name": name,
                "configured": is_configured,
                "prefix": prefix_by_provider.get(name),  # None for anthropic (bare model names route there)
            }
            for name, is_configured in configured.items()
        ]
    }


def _raise_for_write_result(result: WriteResult) -> None:
    if result.ok:
        return
    status = {"validation": 400, "not_found": 404, "collision": 409}.get(result.error_type, 500)
    # Carry the error type explicitly rather than making callers (e.g. the
    # gateway MCP's error mapping) guess it from status code + message text.
    detail = {"type": result.error_type, "message": result.error_message, **result.error_detail}
    raise HTTPException(status_code=status, detail=detail)


_AGENT_WRITE_NOTE = "Files written and reloaded. agents/ is gitignored, so this is not a git-commit concern."


@app.post("/agents")
async def create_agent(request: Request):
    body = await request.json()
    name = body.get("name")
    agent_yaml_text = body.get("agent_yaml")
    soul_md_text = body.get("soul_md")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="Body must include a 'name' string field")
    if not isinstance(agent_yaml_text, str) or not isinstance(soul_md_text, str):
        raise HTTPException(status_code=400, detail="Body must include 'agent_yaml' and 'soul_md' string fields")
    overwrite = bool(body.get("overwrite", False))
    config: GatewayConfig = _state["config"]

    result = write_agent(config.agents_dir, config, name, agent_yaml_text, soul_md_text,
                          is_update=False, overwrite=overwrite)
    _raise_for_write_result(result)
    _state["reload_fn"]()
    return {"agent": result.config, "committed": False, "note": _AGENT_WRITE_NOTE}


@app.put("/agents/{agent_name}")
async def update_agent(agent_name: str, request: Request):
    body = await request.json()
    agent_yaml_text = body.get("agent_yaml")
    soul_md_text = body.get("soul_md")
    if agent_yaml_text is None and soul_md_text is None:
        raise HTTPException(status_code=400, detail="Body must include at least one of 'agent_yaml'/'soul_md'")

    config: GatewayConfig = _state["config"]
    agent_dir = Path(config.agents_dir) / agent_name
    if not (agent_dir / "agent.yaml").exists():
        raise HTTPException(status_code=404, detail={"type": "not_found",
                                                       "message": f"Agent '{agent_name}' not found"})
    # Allow updating just one file — fill the other in from what's already on disk.
    if agent_yaml_text is None:
        agent_yaml_text = (agent_dir / "agent.yaml").read_text()
    if soul_md_text is None:
        soul_path = agent_dir / "soul.md"
        soul_md_text = soul_path.read_text() if soul_path.exists() else ""

    result = write_agent(config.agents_dir, config, agent_name, agent_yaml_text, soul_md_text, is_update=True)
    _raise_for_write_result(result)
    _state["reload_fn"]()
    return {"agent": result.config, "committed": False, "note": _AGENT_WRITE_NOTE}


@app.post("/agents/validate")
async def validate_agent_endpoint(request: Request):
    body = await request.json()
    agent_yaml_text = body.get("agent_yaml")
    soul_md_text = body.get("soul_md", "")
    if not isinstance(agent_yaml_text, str):
        raise HTTPException(status_code=400, detail="Body must include an 'agent_yaml' string field")
    if not isinstance(soul_md_text, str):
        soul_md_text = ""
    config: GatewayConfig = _state["config"]
    valid, errors = validate_agent_files(config.agents_dir, config, agent_yaml_text, soul_md_text)
    return {"valid": valid, "errors": errors}


@app.delete("/agents/{agent_name}")
async def delete_agent_endpoint(agent_name: str):
    config: GatewayConfig = _state["config"]
    result = delete_agent_files(config.agents_dir, agent_name)
    _raise_for_write_result(result)
    _state["reload_fn"]()
    return {
        "deleted": agent_name,
        "agent_yaml": result.config["agent_yaml"],
        "soul_md": result.config["soul_md"],
        "committed": False,
        "note": "Directory removed and reloaded. agents/ is gitignored, so this is not a git-commit concern.",
    }


@app.post("/reload")
async def reload_agents():
    _state["reload_fn"]()
    agents: dict[str, AgentConfig] = _state["agents"]
    return {"agents": [{"name": a.name, "model": a.model} for a in agents.values()]}


@app.get("/health")
async def health():
    config: GatewayConfig = _state["config"]
    mcp_manager: MCPManager = _state["mcp_manager"]
    agents: dict[str, AgentConfig] = _state["agents"]
    run_semaphore: asyncio.Semaphore = _state["run_semaphore"]

    max_runs = config.limits.max_concurrent_runs
    active_runs = max_runs - run_semaphore._value

    server_status = mcp_manager.get_server_status()
    all_mcp_running = all(s["running"] for s in server_status.values()) if server_status else True

    return {
        "status": "ok" if all_mcp_running else "degraded",
        "version": "0.5.0",
        "agents": len(agents),
        "active_runs": active_runs,
        "max_concurrent_runs": max_runs,
        "mcp_servers": {
            name: {"running": s["running"], "restart_count": s["restart_count"]}
            for name, s in server_status.items()
        },
    }


@app.get("/mcp/tools")
async def mcp_tools():
    manager: MCPManager = _state["mcp_manager"]
    return manager.get_all_tools()


@app.get("/mcp/servers")
async def mcp_servers():
    manager: MCPManager = _state["mcp_manager"]
    return manager.get_server_status()


async def _wait_for_disconnect(ws: WebSocket) -> None:
    """Block until the client disconnects.

    The WS protocol only supports one in-flight request per connection, so any
    non-disconnect message received here is unexpected — log it and keep
    waiting rather than treating it as a signal.
    """
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        logging.getLogger(__name__).warning(
            "Unexpected message received while an agent run was in flight — ignoring: %r",
            message,
        )


@app.websocket("/rpc")
async def rpc(ws: WebSocket):
    await ws.accept()

    nonce = secrets.token_hex(16)
    await ws.send_json({
        "type": "event",
        "event": "challenge",
        "payload": {"nonce": nonce},
    })

    authenticated = False
    operator_token: str = _state["operator_token"]

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")
            msg_id = msg.get("id", "")
            method = msg.get("method", "")
            params = msg.get("params", {})

            if msg_type != "req":
                continue

            if not authenticated:
                if method == "connect":
                    token = (params.get("auth") or {}).get("token", "")
                    if token == operator_token:
                        authenticated = True
                        await ws.send_json({
                            "type": "res",
                            "id": msg_id,
                            "ok": True,
                            "payload": {"protocol": 3},
                        })
                    else:
                        await ws.send_json({
                            "type": "res",
                            "id": msg_id,
                            "ok": False,
                            "error": {"message": "invalid operator token"},
                        })
                else:
                    await ws.send_json({
                        "type": "res",
                        "id": msg_id,
                        "ok": False,
                        "error": {"message": "not authenticated"},
                    })
                continue

            if method == "agent":
                agent_id = params.get("agentId", "")
                session_key = params.get("sessionKey", "")
                agents: dict[str, AgentConfig] = _state["agents"]

                if agent_id not in agents:
                    await ws.send_json({
                        "type": "res",
                        "id": msg_id,
                        "ok": False,
                        "error": {"message": f"agent not found: {agent_id}"},
                    })
                    continue

                agent = agents[agent_id]
                session_manager: SessionManager = _state["session_manager"]
                key_error = session_manager.validate_key(session_key, agent_id)
                if key_error:
                    await ws.send_json({
                        "type": "res",
                        "id": msg_id,
                        "ok": False,
                        "error": {"message": key_error},
                    })
                    continue

                session_manager.get_or_create(session_key, agent_id)
                run_id = str(uuid.uuid4())

                # Frame 1: accepted — sent immediately before the runner starts
                await ws.send_json({
                    "type": "res",
                    "id": msg_id,
                    "ok": True,
                    "payload": {"status": "accepted", "runId": run_id},
                })

                runner: AgentRunner = _state["agent_runner"]
                mcp_manager: MCPManager = _state["mcp_manager"]
                config: GatewayConfig = _state["config"]
                run_semaphore: asyncio.Semaphore = _state["run_semaphore"]

                message_text = params.get("message", "")
                model_override = params.get("model") or None
                thinking_level = params.get("thinkingLevel") or None
                trace_tool_result_max = params.get("traceToolResultMax") or None
                remote_ctx = extract_remote_context(params)

                async def _send_trace_event(event: dict) -> None:
                    await ws.send_json({
                        "type": "res",
                        "id": msg_id,
                        "ok": True,
                        "payload": {"status": "trace_event", "event": event},
                    })

                async def _run_agent():
                    # Gate total concurrent runs gateway-wide. Acquiring queues
                    # rather than rejects once the gateway is at capacity.
                    async with run_semaphore:
                        return await runner.run(
                            agent=agent,
                            session_key=session_key,
                            message=message_text,
                            model_override=model_override,
                            thinking_level=thinking_level,
                            mcp_manager=mcp_manager,
                            limits=config.limits,
                            on_trace_event=_send_trace_event,
                            remote_context=remote_ctx,
                            trace_tool_result_max=trace_tool_result_max,
                        )

                run_task = asyncio.create_task(_run_agent())
                disconnect_task = asyncio.create_task(_wait_for_disconnect(ws))

                done, _ = await asyncio.wait(
                    {run_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
                )

                if disconnect_task in done:
                    # Client disconnected (whether queued or already running) —
                    # cancel instead of letting it keep burning LLM/tool calls
                    # for a response nobody will receive.
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logging.getLogger(__name__).warning(
                            "Agent run for %s errored after client disconnect: %s",
                            agent_id, exc,
                        )
                    logging.getLogger(__name__).info(
                        "Client disconnected mid-run for agent %s (run %s) — cancelled",
                        agent_id, run_id,
                    )
                    break

                disconnect_task.cancel()
                with suppress(asyncio.CancelledError):
                    await disconnect_task

                try:
                    result = await run_task

                    # Frame 2: ok with real result
                    await ws.send_json({
                        "type": "res",
                        "id": msg_id,
                        "ok": True,
                        "payload": {
                            "status": "ok",
                            "result": {
                                "payloads": [
                                    {"text": result.text, "mediaUrl": None}
                                ],
                                "meta": {
                                    "durationMs": result.duration_ms,
                                    "agentMeta": {
                                        "provider": result.provider,
                                        "model": result.model_used,
                                        "usage": result.usage,
                                    },
                                    "aborted": False,
                                },
                                "trace": result.trace,
                            },
                        },
                    })

                except AgentRunError as exc:
                    await ws.send_json({
                        "type": "res",
                        "id": msg_id,
                        "ok": False,
                        "error": {"message": str(exc)},
                    })
                except Exception as exc:
                    logging.getLogger(__name__).exception(
                        "Unexpected error running agent %s", agent_id
                    )
                    await ws.send_json({
                        "type": "res",
                        "id": msg_id,
                        "ok": False,
                        "error": {"message": f"internal error: {exc}"},
                    })

            else:
                await ws.send_json({
                    "type": "res",
                    "id": msg_id,
                    "ok": False,
                    "error": {"message": f"unknown method: {method}"},
                })

    except WebSocketDisconnect:
        pass
