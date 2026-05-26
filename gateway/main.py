import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from gateway.auth.device import bootstrap_identity, get_operator_token
from gateway.models.config import GatewayConfig, load_config

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

    _state["config"] = config
    _state["operator_token"] = operator_token

    yield


app = FastAPI(lifespan=lifespan)


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
                await ws.send_json({
                    "type": "res",
                    "id": msg_id,
                    "ok": False,
                    "error": {"message": "not implemented"},
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
