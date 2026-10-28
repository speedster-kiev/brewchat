"""Session timing log (SQLite): first message and order-list builds per anonymous session.

Used for the time-to-order measurement; the manual part of the timing is
recorded by hand against the same ``session_id``. Each row also keeps the
build (app version, prompt version, model) of the session's latest message.
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
    lists_built INTEGER NOT NULL DEFAULT 0,
    app_version TEXT,
    prompt_version TEXT,
    model TEXT
)
"""
BUILD_COLUMNS = ("app_version", "prompt_version", "model")


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
            # Databases created before issue #20 lack the build columns.
            have = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            for col in BUILD_COLUMNS:
                if col not in have:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
            conn.commit()
            self._initialized = True
        return conn

    def record_message(
        self, session_id: str, build: dict[str, str] | None = None, now: datetime | None = None
    ) -> None:
        """Note a user message; ``started_at`` is set on the first one only, the build on every one."""
        build = build or {}
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at, lists_built) VALUES (?, ?, 0)",
                (session_id, _iso(now)),
            )
            if build:
                conn.execute(
                    "UPDATE sessions SET app_version = ?, prompt_version = ?, model = ? WHERE session_id = ?",
                    (*(build.get(c) for c in BUILD_COLUMNS), session_id),
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
                "SELECT session_id, started_at, last_list_built_at, lists_built,"
                " app_version, prompt_version, model FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row[0],
            "started_at": row[1],
            "last_list_built_at": row[2],
            "lists_built": row[3],
            **dict(zip(BUILD_COLUMNS, row[4:], strict=True)),
        }
