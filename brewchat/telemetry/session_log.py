"""Session timing log (SQLite): first message and order-list builds per anonymous session.

Used for the time-to-order measurement; the manual part of the timing is
recorded by hand against the same ``session_id``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    last_list_built_at TEXT,
    lists_built INTEGER NOT NULL DEFAULT 0
)
"""


def _iso(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).isoformat()


class SessionLog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        # A fresh connection per call: tools may run on worker threads.
        if not self._initialized:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        if not self._initialized:
            conn.execute(_SCHEMA)
            conn.commit()
            self._initialized = True
        return conn

    def record_message(self, session_id: str, now: datetime | None = None) -> None:
        """Note a user message; ``started_at`` is set on the first one only."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at, lists_built) VALUES (?, ?, 0)",
                (session_id, _iso(now)),
            )

    def record_list_built(self, session_id: str, now: datetime | None = None) -> None:
        ts = _iso(now)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at, lists_built) VALUES (?, ?, 0)",
                (session_id, ts),
            )
            conn.execute(
                "UPDATE sessions SET last_list_built_at = ?, lists_built = lists_built + 1"
                " WHERE session_id = ?",
                (ts, session_id),
            )

    def get(self, session_id: str) -> dict[str, Any] | None:
        if not self.db_path.is_file():
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT session_id, started_at, last_list_built_at, lists_built FROM sessions"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row[0],
            "started_at": row[1],
            "last_list_built_at": row[2],
            "lists_built": row[3],
        }
