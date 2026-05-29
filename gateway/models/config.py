import os
import re
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class LimitsConfig(BaseModel):
    max_agent_iterations: int = 20
    request_timeout_seconds: int = 180
    mcp_tool_timeout_seconds: int = 30


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 18789


class IdentityConfig(BaseModel):
    path: str = "~/.pork-gateway/identity"


class MCPServerConfig(BaseModel):
    command: str
    args: list[str] = []
    env: dict[str, str] = {}


class ProviderConfig(BaseModel):
    api_key: str = ""
    base_url: Optional[str] = None


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig, alias="ollama-local")
    ollama_cloud: ProviderConfig = Field(default_factory=ProviderConfig, alias="ollama-cloud")
    google: ProviderConfig = Field(default_factory=ProviderConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"


class GatewayConfig(BaseModel):
    server: ServerConfig
    agents_dir: str = "./agents"
    identity: IdentityConfig
    limits: LimitsConfig = LimitsConfig()
    mcp_servers: dict[str, MCPServerConfig] = {}
    providers: ProvidersConfig
    logging: LoggingConfig = LoggingConfig()


def _resolve_env_vars(obj):
    """Recursively replace ${VAR_NAME} patterns with environment variable values."""
    if isinstance(obj, str):
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda m: os.environ.get(m.group(1), ""),
            obj,
        )
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(item) for item in obj]
    return obj


def load_config(path: str = "config.yaml") -> GatewayConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    resolved = _resolve_env_vars(raw)
    return GatewayConfig(**resolved)
