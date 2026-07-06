import asyncio
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from gateway.agents.loader import install_sighup_handler, load_agents, validate_agent_models
from gateway.auth.device import bootstrap_identity, get_operator_token
from gateway.llm.router import LLMRouter
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

                    # Derive provider name from the model string used
                    effective_model = model_override or agent.model
                    if effective_model.startswith("openrouter/"):
                        provider_name = "openrouter"
                    elif effective_model.startswith("ollama/"):
                        provider_name = "ollama"
                    elif effective_model.startswith("ollama-cloud/"):
                        provider_name = "ollama-cloud"
                    elif effective_model.startswith("google/"):
                        provider_name = "google"
                    else:
                        provider_name = "anthropic"

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
                                        "provider": provider_name,
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
