from pydantic import BaseModel


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
