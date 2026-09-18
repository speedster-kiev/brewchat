"""Per-session state and the contextvar through which tools reach it.

The runner sets ``current_session`` before running the tool loop; tool
functions read it instead of taking a session argument the model could forge.
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from brewchat.agent.models import Ingredient, OrderList


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict[str, Any]] = field(default_factory=list)
    parsed_recipe: list[Ingredient] | None = None
    order_list: OrderList | None = None
    # original_name -> proposed product handle/title from the most recent build,
    # so a later build can tell "rejected" from a fresh "unavailable".
    proposals: dict[str, dict[str, str]] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_seen = time.monotonic()


current_session: ContextVar[Session] = ContextVar("current_session")
