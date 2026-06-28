import logging
import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from gateway.models.agent import AgentConfig
from gateway.models.config import GatewayConfig

logger = logging.getLogger(__name__)

# Maps the model-string prefix (as used in agent.yaml) to the provider field name
# on ProvidersConfig. Must stay in sync with LLMRouter.get_provider_and_model.
_PREFIX_TO_PROVIDER: dict[str, str] = {
    "anthropic/": "anthropic",
    "openrouter/": "openrouter",
    "ollama/": "ollama",         # ollama-local in config
    "ollama-cloud/": "ollama_cloud",
    "google/": "google",
    "azure/": "azure",
}
# Providers that work without an api_key (local Ollama uses no auth by default).
_KEY_NOT_REQUIRED = {"ollama"}


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
            raw = yaml.safe_load(yaml_path.read_text())
        except Exception as exc:
            logger.error("Failed to parse %s: %s — skipping", yaml_path, exc)
            continue

        if raw.get("name") != dir_name:
            logger.error(
                "Agent YAML name '%s' does not match directory '%s' — skipping",
                raw.get("name"),
                dir_name,
            )
            continue

        try:
            agent = AgentConfig(**raw, soul=soul_path.read_text())
        except ValidationError as exc:
            logger.error("Agent '%s' failed validation: %s — skipping", dir_name, exc)
            continue

        agents[agent.name] = agent
        logger.info("Loaded agent: %s (model: %s)", agent.name, agent.model)

    return agents


def validate_agent_models(agents: dict[str, AgentConfig], config: GatewayConfig) -> None:
    """Log errors/warnings for model strings that reference unknown or unconfigured providers.

    Called at startup and on hot reload so misconfigurations surface immediately
    rather than as a KeyError at first request time.
    """
    providers = config.providers
    key_configured: dict[str, bool] = {
        "anthropic": bool(providers.anthropic.api_key),
        "openrouter": bool(providers.openrouter.api_key),
        "ollama": True,  # local Ollama — no API key needed
        "ollama_cloud": bool(providers.ollama_cloud.api_key),
        "google": bool(providers.google.api_key),
        "azure": bool(providers.azure.api_key and providers.azure.resource_name),
    }

    for agent_name, agent in agents.items():
        for model_string in [agent.model, *agent.model_fallbacks]:
            provider_key: str | None = None
            for prefix, pkey in _PREFIX_TO_PROVIDER.items():
                if model_string.startswith(prefix):
                    provider_key = pkey
                    break

            if provider_key is None:
                if "/" in model_string:
                    # Has a slash-separated prefix but it doesn't match any known provider.
                    logger.error(
                        "Agent '%s': model '%s' uses an unrecognized provider prefix"
                        " — will fail at request time. Known prefixes: %s",
                        agent_name,
                        model_string,
                        ", ".join(p.rstrip("/") for p in _PREFIX_TO_PROVIDER),
                    )
                    continue
                else:
                    # Bare model name (no prefix) — routes to Anthropic by default.
                    provider_key = "anthropic"

            if provider_key not in _KEY_NOT_REQUIRED and not key_configured.get(provider_key, False):
                logger.warning(
                    "Agent '%s': model '%s' uses provider '%s' but its api_key is not"
                    " configured — requests will likely fail with auth errors",
                    agent_name,
                    model_string,
                    provider_key.replace("_", "-"),
                )


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
