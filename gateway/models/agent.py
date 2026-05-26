from pydantic import BaseModel


class AgentConfig(BaseModel):
    name: str
    model: str
    max_tokens: int = 8192
    tools: list[str] = []
    soul: str = ""  # loaded content of soul.md
