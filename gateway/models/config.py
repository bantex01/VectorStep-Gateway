import os
import re
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class LimitsConfig(BaseModel):
    max_agent_iterations: int = 20
    request_timeout_seconds: int = 180
    mcp_tool_timeout_seconds: int = 30
    llm_retry_attempts: int = 2          # retries on the *same* model before falling over
    llm_retry_base_delay_seconds: float = 1.0  # doubles each attempt (exponential backoff)
    max_concurrent_runs: int = 10        # gateway-wide cap on simultaneously executing agent runs
    trace_tool_result_max_chars: int = 3000  # gateway-wide default; a caller can override
                                          # per-request via the run request's traceToolResultMax —
                                          # this only caps the TRACE copy of a tool result (what's
                                          # streamed/persisted for observability and any downstream
                                          # judge to inspect); the LLM conversation itself always
                                          # gets the tool's full, untruncated output regardless of
                                          # this setting (see agent_runner.py's _emit vs the actual
                                          # tool_results appended to `messages`).


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 18789


class IdentityConfig(BaseModel):
    path: str = "~/.vectorstep-gateway/identity"


class MCPServerConfig(BaseModel):
    command: str
    args: list[str] = []
    env: dict[str, str] = {}


class ProviderConfig(BaseModel):
    api_key: str = ""
    base_url: Optional[str] = None


class AzureConfig(BaseModel):
    api_key: str = ""
    resource_name: str = ""          # subdomain of .openai.azure.com, e.g. "my-company-openai"
    api_version: str = "2025-01-01-preview"


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig, alias="ollama-local")
    ollama_cloud: ProviderConfig = Field(default_factory=ProviderConfig, alias="ollama-cloud")
    google: ProviderConfig = Field(default_factory=ProviderConfig)
    azure: AzureConfig = Field(default_factory=AzureConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    yolo: ProviderConfig = Field(default_factory=ProviderConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    # Empty/omitted disables file logging (stdout only) — see gateway.main._setup_logging.
    # When set, creates gateway.log and access.log (rotating, 10 MB x 5), mirroring
    # VectorStep's service.log/access.log split.
    dir: str = ""


class OtelConfig(BaseModel):
    enabled: bool = False
    exporter: str = "otlp"        # otlp | console
    endpoint: str = "http://localhost:4318/v1/traces"
    service_name: str = "vectorstep-gateway"


class ObservabilityConfig(BaseModel):
    otel: OtelConfig = OtelConfig()


class GatewayConfig(BaseModel):
    server: ServerConfig
    agents_dir: str = "./agents"
    identity: IdentityConfig
    limits: LimitsConfig = LimitsConfig()
    mcp_servers: dict[str, MCPServerConfig] = {}
    providers: ProvidersConfig
    logging: LoggingConfig = LoggingConfig()
    observability: ObservabilityConfig = ObservabilityConfig()


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
