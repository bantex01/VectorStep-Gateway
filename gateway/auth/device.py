import json
import os
import uuid
from pathlib import Path

from gateway.models.config import IdentityConfig


def _identity_dir(config: IdentityConfig) -> Path:
    """Return resolved identity directory, allowing env var override."""
    env_override = os.environ.get("VECTORSTEP_GATEWAY_IDENTITY_DIR")
    raw = env_override if env_override else config.path
    return Path(raw).expanduser().resolve()


def bootstrap_identity(config: IdentityConfig) -> dict:
    """
    Generate identity files on first run, or load existing ones.
    Returns the loaded auth data dict.
    """
    identity_dir = _identity_dir(config)
    device_json = identity_dir / "device.json"
    auth_json = identity_dir / "device-auth.json"

    if not device_json.exists() or not auth_json.exists():
        identity_dir.mkdir(parents=True, exist_ok=True)

        device_id = str(uuid.uuid4())
        operator_token = str(uuid.uuid4())

        device_data = {"deviceId": device_id, "privateKeyPem": ""}
        auth_data = {
            "tokens": {
                "operator": {
                    "token": operator_token,
                    "scopes": ["agent:invoke", "gateway:connect"],
                }
            }
        }

        device_json.write_text(json.dumps(device_data, indent=2))
        auth_json.write_text(json.dumps(auth_data, indent=2))

        print(f"[vectorstep-gateway] First run — generated device identity.")
        print(f"[vectorstep-gateway] Written to: {device_json}")
        print(f"[vectorstep-gateway] Written to: {auth_json}")
        print(f"[vectorstep-gateway] Add the following to your VectorStep config.yaml:")
        print(f"[vectorstep-gateway]   executors:")
        print(f"[vectorstep-gateway]     vectorstep_gateway:")
        print(f"[vectorstep-gateway]       url: ws://localhost:18789/rpc")
        print(f"[vectorstep-gateway]       identity_dir: {identity_dir}")
        print(f"[vectorstep-gateway] Then use executor: vectorstep_gateway in your pipeline YAML steps.")

        return auth_data

    return json.loads(auth_json.read_text())


def get_operator_token(auth_data: dict) -> str:
    return auth_data["tokens"]["operator"]["token"]
