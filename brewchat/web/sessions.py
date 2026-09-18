"""In-memory chat sessions with idle expiry. Single-process demo; nothing persisted."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from brewchat.agent.session import Session

IDLE_TIMEOUT_SECONDS = 60 * 60


class SessionStore:
    def __init__(
        self,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_timeout = idle_timeout
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._turn_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __contains__(self, session_id: object) -> bool:
        with self._lock:
            return session_id in self._sessions

    def sweep(self) -> int:
        """Drop sessions idle longer than the timeout. Returns how many were dropped."""
        with self._lock:
            return self._sweep_locked()

    def _sweep_locked(self) -> int:
        cutoff = self._clock() - self.idle_timeout
        stale = [sid for sid, s in self._sessions.items() if s.last_seen < cutoff]
        for sid in stale:
            del self._sessions[sid]
            self._turn_locks.pop(sid, None)
        return len(stale)

    def get_or_create(self, session_id: str | None) -> Session:
        """Return the live session for ``session_id``, or a fresh one with a new id.

        Client-supplied ids are never adopted for new sessions: an unknown or
        expired id always yields a server-generated one.
        """
        with self._lock:
            self._sweep_locked()
            session = self._sessions.get(session_id) if session_id else None
            if session is None:
                session = Session()
                self._sessions[session.session_id] = session
                self._turn_locks[session.session_id] = threading.Lock()
            session.last_seen = self._clock()
            return session

    def turn_lock(self, session: Session) -> threading.Lock:
        """Per-session lock so two concurrent turns cannot interleave one history."""
        with self._lock:
            return self._turn_locks.setdefault(session.session_id, threading.Lock())
