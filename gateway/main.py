import asyncio
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from gateway.agents.loader import install_sighup_handler, load_agents
from gateway.auth.device import bootstrap_identity, get_operator_token
from gateway.llm.router import LLMRouter
from gateway.mcp.manager import MCPManager
from gateway.models.agent import AgentConfig
from gateway.models.config import GatewayConfig, load_config
from gateway.runner.agent_runner import AgentRunError, AgentRunner
from gateway.session.manager import SessionManager

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = os.environ.get("PORK_GATEWAY_CONFIG", "config.yaml")
    config: GatewayConfig = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    auth_data = bootstrap_identity(config.identity)
    operator_token = get_operator_token(auth_data)

    agents = load_agents(config.agents_dir)
    session_manager = SessionManager()

    mcp_manager = MCPManager(config)
    await mcp_manager.start_all()

    llm_router = LLMRouter(config)
    agent_runner = AgentRunner(llm_router)

    _state["config"] = config
    _state["operator_token"] = operator_token
    _state["agents"] = agents
    _state["session_manager"] = session_manager
    _state["mcp_manager"] = mcp_manager
    _state["agent_runner"] = agent_runner

    def _reload():
        new_agents = load_agents(config.agents_dir)
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


app = FastAPI(lifespan=lifespan)


@app.get("/agents")
async def list_agents():
    agents: dict[str, AgentConfig] = _state.get("agents", {})
    return {"agents": [{"name": a.name, "model": a.model} for a in agents.values()]}


@app.post("/reload")
async def reload_agents():
    _state["reload_fn"]()
    agents: dict[str, AgentConfig] = _state["agents"]
    return {"agents": [{"name": a.name, "model": a.model} for a in agents.values()]}


@app.get("/mcp/tools")
async def mcp_tools():
    manager: MCPManager = _state["mcp_manager"]
    return manager.get_all_tools()


@app.get("/mcp/servers")
async def mcp_servers():
    manager: MCPManager = _state["mcp_manager"]
    return manager.get_server_status()


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

                message_text = params.get("message", "")
                model_override = params.get("model") or None
                thinking_level = params.get("thinkingLevel") or None

                try:
                    result = await runner.run(
                        agent=agent,
                        session_key=session_key,
                        message=message_text,
                        model_override=model_override,
                        thinking_level=thinking_level,
                        mcp_manager=mcp_manager,
                        limits=config.limits,
                    )

                    # Derive provider name from the model string used
                    effective_model = model_override or agent.model
                    if effective_model.startswith("openrouter/"):
                        provider_name = "openrouter"
                    elif effective_model.startswith("ollama/"):
                        provider_name = "ollama"
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
