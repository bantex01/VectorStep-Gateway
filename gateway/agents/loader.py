import logging
import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from gateway.models.agent import AgentConfig

logger = logging.getLogger(__name__)


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
