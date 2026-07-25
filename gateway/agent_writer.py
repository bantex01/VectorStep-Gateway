"""Atomic validated-write path for agent.yaml + soul.md pairs
(SPEC-gateway-mcp.md §3's "atomic write" note). This is the ONE place that
writes to agents/ for the create/update/delete JSON endpoints — a single
tested write path shared by any future caller (the gateway MCP, or a future
UI editor).

Mirrors P-Ork's service/src/config_writer.py: every write validates a
*candidate* view of agents/ — the real directories, plus the one
new/changed/removed entry — fully in memory via
gateway.agents.loader.load_agents_from_raw()/validate_agent_models(), before
anything touches disk. Only if that candidate build succeeds does the real
atomic write happen (temp file + os.replace()), so a crash mid-write can't
leave a half-written or invalid agent directory for a concurrent reload to
pick up.
"""

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from gateway.agents.loader import load_agents_from_raw, validate_agent_models
from gateway.models.config import GatewayConfig


@dataclass
class WriteResult:
    ok: bool
    config: dict | None = None
    error_type: str | None = None
    error_message: str | None = None
    error_detail: dict = field(default_factory=dict)


def _read_all_agents(agents_dir: str) -> dict[str, tuple[str, str]]:
    base = Path(agents_dir)
    if not base.is_dir():
        return {}
    result: dict[str, tuple[str, str]] = {}
    for yaml_path in base.glob("*/agent.yaml"):
        soul_path = yaml_path.parent / "soul.md"
        if not soul_path.exists():
            continue  # mirrors load_agents()'s own skip — not part of the live registry
        result[yaml_path.parent.name] = (yaml_path.read_text(), soul_path.read_text())
    return result


def _atomic_write(dir_path: Path, filename: str, content: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    tmp_path = dir_path / f".{filename}.tmp-{uuid.uuid4().hex}"
    tmp_path.write_text(content)
    os.replace(tmp_path, dir_path / filename)


def _validation_errors_from_pydantic(exc: ValidationError) -> list[dict]:
    return [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]


def _parse_named_yaml(agent_yaml_text: str, name: str) -> tuple[dict | None, WriteResult | None]:
    try:
        parsed = yaml.safe_load(agent_yaml_text)
    except yaml.YAMLError as exc:
        return None, WriteResult(ok=False, error_type="validation", error_message=f"Invalid YAML: {exc}")
    if not isinstance(parsed, dict) or not parsed.get("name"):
        return None, WriteResult(
            ok=False, error_type="validation",
            error_message="agent_yaml must be a mapping with a 'name' field",
        )
    if parsed["name"] != name:
        return None, WriteResult(
            ok=False, error_type="validation",
            error_message=f"'name' ('{name}') does not match agent_yaml's own name field ('{parsed['name']}')",
        )
    return parsed, None


def _model_errors_for(name: str, candidate_agents: dict, config: GatewayConfig) -> list[dict]:
    """error-severity validate_agent_models() entries for just this agent — a
    write only fails on problems introduced by the agent being written, not
    pre-existing issues on unrelated agents already in the registry."""
    return [
        e for e in validate_agent_models(candidate_agents, config)
        if e["agent"] == name and e["severity"] == "error"
    ]


def write_agent(
    agents_dir: str,
    config: GatewayConfig,
    name: str,
    agent_yaml_text: str,
    soul_md_text: str,
    *,
    is_update: bool,
    overwrite: bool = False,
) -> WriteResult:
    """Validate and atomically write an agent's agent.yaml + soul.md.

    `name` is authoritative for the target directory (the URL {name} for a
    PUT, or the body's `name` field for a POST) and must match agent_yaml's
    own `name:` field — a rename is a delete + create, not an update, same
    rationale as P-Ork's pipeline PUT.
    """
    parsed, err = _parse_named_yaml(agent_yaml_text, name)
    if err:
        return err

    agent_dir = Path(agents_dir) / name
    exists = (agent_dir / "agent.yaml").exists()
    if is_update and not exists:
        return WriteResult(ok=False, error_type="not_found", error_message=f"Agent '{name}' not found")
    if not is_update and exists and not overwrite:
        return WriteResult(ok=False, error_type="collision", error_message=f"Agent '{name}' already exists")

    candidate = _read_all_agents(agents_dir)
    candidate[name] = (agent_yaml_text, soul_md_text)
    try:
        candidate_agents = load_agents_from_raw(candidate, log_success=False)
    except ValidationError as exc:
        return WriteResult(ok=False, error_type="validation", error_message=str(exc),
                            error_detail={"errors": _validation_errors_from_pydantic(exc)})
    except Exception as exc:
        return WriteResult(ok=False, error_type="validation", error_message=str(exc))

    model_errors = _model_errors_for(name, candidate_agents, config)
    if model_errors:
        return WriteResult(
            ok=False, error_type="validation",
            error_message=f"Agent '{name}' references an unconfigured model or MCP tool server",
            error_detail={"errors": model_errors},
        )

    _atomic_write(agent_dir, "agent.yaml", agent_yaml_text)
    _atomic_write(agent_dir, "soul.md", soul_md_text)
    return WriteResult(ok=True, config={"name": name, "agent_yaml": agent_yaml_text, "soul_md": soul_md_text})


def delete_agent(agents_dir: str, name: str) -> WriteResult:
    """Delete an agent's directory, returning the prior agent.yaml/soul.md
    content for audit. No candidate-reload check is needed: agents don't
    reference each other, so removing one can't break another's resolution."""
    agent_dir = Path(agents_dir) / name
    yaml_path = agent_dir / "agent.yaml"
    soul_path = agent_dir / "soul.md"
    if not yaml_path.exists():
        return WriteResult(ok=False, error_type="not_found", error_message=f"Agent '{name}' not found")

    prior_yaml = yaml_path.read_text()
    prior_soul = soul_path.read_text() if soul_path.exists() else ""

    yaml_path.unlink()
    if soul_path.exists():
        soul_path.unlink()
    try:
        agent_dir.rmdir()  # only succeeds if empty — leaves any stray extra files in place
    except OSError:
        pass

    return WriteResult(ok=True, config={"name": name, "agent_yaml": prior_yaml, "soul_md": prior_soul})


def validate_agent(
    agents_dir: str, config: GatewayConfig, agent_yaml_text: str, soul_md_text: str,
) -> tuple[bool, list[dict]]:
    """Dry-run validation of a candidate agent.yaml + soul.md pair — no write."""
    try:
        parsed = yaml.safe_load(agent_yaml_text)
    except yaml.YAMLError as exc:
        return False, [{"loc": [], "msg": f"Invalid YAML: {exc}"}]
    if not isinstance(parsed, dict) or not parsed.get("name"):
        return False, [{"loc": [], "msg": "agent_yaml must be a mapping with a 'name' field"}]
    name = parsed["name"]

    candidate = _read_all_agents(agents_dir)
    candidate[name] = (agent_yaml_text, soul_md_text)
    try:
        candidate_agents = load_agents_from_raw(candidate, log_success=False)
    except ValidationError as exc:
        return False, _validation_errors_from_pydantic(exc)
    except Exception as exc:
        return False, [{"loc": [], "msg": str(exc)}]

    model_errors = _model_errors_for(name, candidate_agents, config)
    if model_errors:
        return False, [
            {"loc": [e["field"]], "msg": e["message"]} for e in model_errors
        ]

    return True, []
