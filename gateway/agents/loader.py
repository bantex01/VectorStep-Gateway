import logging
import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from gateway.llm.router import (
    PREFIX_TO_PROVIDER,
    THINKING_CAPABLE_PROVIDERS,
    provider_key_for_model_string,
)
from gateway.models.agent import AgentConfig
from gateway.models.config import GatewayConfig

logger = logging.getLogger(__name__)

# Providers that work without an api_key (local Ollama uses no auth by default).
_KEY_NOT_REQUIRED = {"ollama"}


def provider_configured_map(config: GatewayConfig) -> dict[str, bool]:
    """Which provider keys have credentials configured (no key values returned).

    Shared by validate_agent_models (below) and the GET /providers endpoint, so
    "is this provider usable" is defined in exactly one place.
    """
    providers = config.providers
    return {
        "anthropic": bool(providers.anthropic.api_key),
        "openrouter": bool(providers.openrouter.api_key),
        "ollama": True,  # local Ollama — no API key needed
        "ollama_cloud": bool(providers.ollama_cloud.api_key),
        "google": bool(providers.google.api_key),
        "azure": bool(providers.azure.api_key and providers.azure.resource_name),
        "openai": bool(providers.openai.api_key),
        "yolo": bool(providers.yolo.api_key),
    }


def _build_agent(dir_name: str, agent_yaml_text: str, soul_md_text: str) -> AgentConfig:
    """Parse+validate one agent.yaml/soul.md pair. Raises on any problem
    (yaml.YAMLError, ValueError for shape/name-mismatch, pydantic.ValidationError
    for schema errors) rather than swallowing it — callers decide whether to let
    that propagate (load_agents_from_raw, the strict candidate-build path) or
    catch-and-skip it (load_agents, the best-effort disk scan)."""
    raw = yaml.safe_load(agent_yaml_text)
    if not isinstance(raw, dict):
        raise ValueError(f"Agent YAML for '{dir_name}' must be a mapping")
    if raw.get("name") != dir_name:
        raise ValueError(
            f"Agent YAML name '{raw.get('name')}' does not match directory '{dir_name}'"
        )
    agent = AgentConfig(**raw, soul=soul_md_text)
    agent.version = agent.compute_version()
    return agent


def load_agents_from_raw(
    raw_by_agent: dict[str, tuple[str, str]],
    log_success: bool = True,
) -> dict[str, AgentConfig]:
    """Build an agent registry from in-memory {dir_name: (agent_yaml_text,
    soul_md_text)} pairs instead of scanning disk. Fails fast — the first
    invalid entry raises rather than being skipped. This is the strict
    counterpart to load_agents()'s best-effort disk scan, used to validate a
    *candidate* agents/ directory (every real agent, plus one new/changed
    entry) before a write endpoint commits anything to disk (see
    gateway/agent_writer.py)."""
    agents: dict[str, AgentConfig] = {}
    for dir_name in sorted(raw_by_agent):
        agent_yaml_text, soul_md_text = raw_by_agent[dir_name]
        agent = _build_agent(dir_name, agent_yaml_text, soul_md_text)
        agents[agent.name] = agent
        if log_success:
            logger.info("Loaded agent: %s (model: %s)", agent.name, agent.model)
    return agents


def load_agents(agents_dir: str) -> dict[str, AgentConfig]:
    base = Path(agents_dir)
    agents: dict[str, AgentConfig] = {}

    for yaml_path in sorted(base.glob("*/agent.yaml")):
        dir_name = yaml_path.parent.name
        soul_path = yaml_path.parent / "soul.md"

        if not soul_path.exists():
            logger.error("Agent '%s' missing soul.md — skipping", dir_name)
            continue

        try:
            agent = _build_agent(dir_name, yaml_path.read_text(), soul_path.read_text())
        except yaml.YAMLError as exc:
            logger.error("Failed to parse %s: %s — skipping", yaml_path, exc)
            continue
        except ValueError as exc:
            logger.error("%s — skipping", exc)
            continue
        except ValidationError as exc:
            logger.error("Agent '%s' failed validation: %s — skipping", dir_name, exc)
            continue

        agents[agent.name] = agent
        logger.info("Loaded agent: %s (model: %s)", agent.name, agent.model)

    return agents


def validate_agent_models(agents: dict[str, AgentConfig], config: GatewayConfig) -> list[dict]:
    """Log errors/warnings for model strings that reference unknown or unconfigured
    providers, and for `tools:` entries that reference an unconfigured MCP server.

    Called at startup and on hot reload so misconfigurations surface immediately
    rather than as a KeyError at first request time — that path only logs, it
    never raises, so a broken agent doesn't take the whole gateway down.

    Also returns a list of the same problems as structured dicts
    ({"agent", "field", "value", "message", "severity"}), so a write endpoint
    (gateway/agent_writer.py) can turn a *newly written* agent's own
    "error"-severity entries into a hard validation failure, without changing
    the soft/log-only behaviour of the startup/reload path. "warning"-severity
    entries (a recognized provider with no api_key yet) never block a write —
    the key may simply not be set yet.
    """
    providers = config.providers
    key_configured = provider_configured_map(config)
    mcp_server_names = set(config.mcp_servers)
    results: list[dict] = []

    for agent_name, agent in agents.items():
        for model_string in [agent.model, *agent.model_fallbacks]:
            provider_key: str | None = None
            for prefix, pkey in PREFIX_TO_PROVIDER.items():
                if model_string.startswith(prefix):
                    provider_key = pkey
                    break

            if provider_key is None:
                if "/" in model_string:
                    # Has a slash-separated prefix but it doesn't match any known provider.
                    msg = (
                        f"Agent '{agent_name}': model '{model_string}' uses an unrecognized"
                        f" provider prefix — will fail at request time. Known prefixes: "
                        + ", ".join(p.rstrip("/") for p in PREFIX_TO_PROVIDER)
                    )
                    logger.error(msg)
                    results.append({"agent": agent_name, "field": "model", "value": model_string,
                                     "message": msg, "severity": "error"})
                    continue
                else:
                    # Bare model name (no prefix) — routes to Anthropic by default.
                    provider_key = "anthropic"

            if provider_key not in _KEY_NOT_REQUIRED and not key_configured.get(provider_key, False):
                msg = (
                    f"Agent '{agent_name}': model '{model_string}' uses provider"
                    f" '{provider_key.replace('_', '-')}' but its api_key is not configured"
                    f" — requests will likely fail with auth errors"
                )
                logger.warning(msg)
                results.append({"agent": agent_name, "field": "model", "value": model_string,
                                 "message": msg, "severity": "warning"})

        # thinking_level is provider-agnostic config, but only some providers act
        # on it. Warn ONCE here rather than leaving the per-call ignore-warning in
        # the provider as the only signal — a 10-iteration agentic run would emit
        # that ten times, and a cross-provider model_fallback makes an agent lose
        # its extended thinking silently mid-run.
        if agent.thinking_level and agent.thinking_level != "off":
            deaf = [
                model_string
                for model_string in [agent.model, *agent.model_fallbacks]
                if provider_key_for_model_string(model_string) not in THINKING_CAPABLE_PROVIDERS
            ]
            if deaf:
                # The primary model being deaf means the agent never thinks at all;
                # only-fallbacks-deaf means it stops thinking mid-run on failover.
                primary_affected = agent.model in deaf
                msg = (
                    f"Agent '{agent_name}': thinking_level='{agent.thinking_level}' is ignored by"
                    f" {', '.join(deaf)} — no reasoning parameter is wired up for"
                    f" {'that provider' if len(deaf) == 1 else 'those providers'}, so this agent "
                    + (
                        "will never use extended thinking."
                        if primary_affected
                        else "silently loses extended thinking when it falls back."
                    )
                )
                logger.warning(msg)
                results.append({"agent": agent_name, "field": "thinking_level",
                                 "value": agent.thinking_level, "message": msg,
                                 "severity": "warning"})

        for server_name in agent.tool_scopes():
            if server_name not in mcp_server_names:
                msg = (
                    f"Agent '{agent_name}': tools reference unconfigured MCP server"
                    f" '{server_name}'"
                )
                logger.error(msg)
                results.append({"agent": agent_name, "field": "tools", "value": server_name,
                                 "message": msg, "severity": "error"})

    return results


def install_sighup_handler(reload_fn) -> None:
    import signal

    def _handler(signum, frame):
        logger.info("SIGHUP received — reloading agents")
        reload_fn()

    try:
        signal.signal(signal.SIGHUP, _handler)
    except (OSError, ValueError):
        # Windows or non-main-thread — skip
        pass
