import hashlib
import json

from pydantic import BaseModel


def normalise_text(text: str) -> str:
    """Conservative normalisation shared with VectorStep's prompt hashing — strip
    trailing whitespace per line, strip leading/trailing blank lines, nothing
    more. See SPEC-prompt-versioning.md §2."""
    return "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")


class AgentConfig(BaseModel):
    name: str
    model: str
    model_fallbacks: list[str] = []  # tried in order if `model` exhausts its retries
    max_tokens: int = 8192
    # Each entry is either a bare server name (grants every tool from that
    # server — original behaviour) or a {server_name: [tool_name, ...]}
    # mapping to scope it down to specific tools.
    tools: list[str | dict[str, list[str]]] = []
    soul: str = ""  # loaded content of soul.md
    version: str = ""  # sha256[:12] over the whole config — set by _build_agent

    def tool_scopes(self) -> dict[str, list[str] | None]:
        """Normalize `tools:` into {server_name: allowed_tool_names}.

        A value of None means "every tool from this server" (unscoped).
        """
        scopes: dict[str, list[str] | None] = {}
        for entry in self.tools:
            if isinstance(entry, str):
                scopes[entry] = None
            else:
                for server_name, tool_names in entry.items():
                    scopes[server_name] = tool_names
        return scopes

    def compute_version(self) -> str:
        """Content hash over this agent's entire behavioural definition.

        DELIBERATELY over-inclusive: hashes every field on this model, not a
        curated subset. Adding ANY new field to AgentConfig therefore resets
        every agent's calibration bucket in VectorStep (SPEC-prompt-versioning.md
        §2). That is the safe failure direction — over-invalidation costs
        labels, under-invalidation corrupts an enforcing gate — but be aware
        of it when adding fields, and prefer to batch such changes.
        """
        payload = self.model_dump(exclude={"version"})
        payload["soul"] = normalise_text(payload.get("soul", ""))
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]
