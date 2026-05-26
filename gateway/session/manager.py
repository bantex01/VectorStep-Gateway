import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Session:
    session_key: str
    agent_name: str
    messages: list = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_key: str, agent_name: str) -> Session:
        if session_key not in self._sessions:
            self._sessions[session_key] = Session(
                session_key=session_key,
                agent_name=agent_name,
            )
            logger.debug("Created session: %s", session_key)
        return self._sessions[session_key]

    def validate_key(self, session_key: str, agent_id: str) -> Optional[str]:
        """Return an error message if the key is invalid, else None."""
        expected_prefix = f"agent:{agent_id}:"
        if not session_key.startswith(expected_prefix):
            return f"sessionKey must start with '{expected_prefix}', got: {session_key}"
        return None
