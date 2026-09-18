"""FastAPI app: passphrase gate, chat page, and ``POST /chat``.

Launch with ``uv run uvicorn brewchat.web.app:app``. The module-level ``app``
is created lazily (module ``__getattr__``) so importing this module, e.g. in
tests, does not require the local supplier config or the passphrase.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from brewchat.agent.models import OrderList
from brewchat.agent.prompt import PromptError
from brewchat.catalog.store import CacheMissingError
from brewchat.config import ConfigError, Settings, load_env_file, load_settings
from brewchat.web.auth import install_auth
from brewchat.web.sessions import SessionStore

if TYPE_CHECKING:
    from brewchat.agent.runner import Runner

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_MESSAGE_CHARS = 50_000


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=100)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    order_list: OrderList | None
    cache_timestamp: str


def _error(status: int, message: str) -> tuple[int, str]:
    return status, message


def _map_runner_error(exc: Exception) -> tuple[int, str]:
    """Translate a failure into a short, safe JSON error. Details go to the server log only."""
    if isinstance(exc, anthropic.RateLimitError):
        return _error(503, "The model is busy right now. Please try again in a minute.")
    if isinstance(exc, anthropic.APITimeoutError):
        return _error(504, "The model took too long to answer. Please try again.")
    if isinstance(exc, anthropic.APIConnectionError):
        return _error(502, "Could not reach the model API. Please try again shortly.")
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return _error(502, "The model API rejected the server's credentials.")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 529:
            return _error(502, "The model API is having trouble. Please try again shortly.")
        return _error(502, "The model API rejected the request.")
    if isinstance(exc, CacheMissingError | FileNotFoundError | sqlite3.Error):
        return _error(503, "The catalog cache is missing or unreadable. Run the catalog sync first.")
    if isinstance(exc, TypeError) and "authentication method" in str(exc):
        return _error(503, "The server has no model API key configured (ANTHROPIC_API_KEY).")
    if isinstance(exc, PromptError | ConfigError):
        return _error(500, "The server is misconfigured.")
    return _error(500, "Something went wrong on our side. Please try again.")


def create_app(settings: Settings | None = None, runner: Runner | None = None) -> FastAPI:
    """Build the app. Raises ``ConfigError`` (refuse to start) without config or passphrase."""
    if settings is None:
        load_env_file()
        settings = load_settings()
    if not settings.passphrase:
        raise ConfigError("BREWCHAT_PASSPHRASE is not set; refusing to start without the access gate.")

    app = FastAPI(title="BrewChat", docs_url=None, redoc_url=None, openapi_url=None)
    install_auth(app, settings.passphrase)
    store = SessionStore()
    app.state.settings = settings
    app.state.sessions = store

    runner_lock = threading.Lock()
    holder: dict[str, Any] = {"runner": runner}

    def get_runner() -> Runner:
        # Built on first use so a missing cache or API key surfaces as a JSON
        # error on /chat rather than a crash at import time.
        with runner_lock:
            if holder["runner"] is None:
                from brewchat.agent.runner import Runner as _Runner
                from brewchat.agent.tools import ToolContext

                holder["runner"] = _Runner(ToolContext.from_settings(settings))
            return holder["runner"]

    app.state.get_runner = get_runner

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        session = store.get_or_create(req.session_id)

        def _turn():
            with store.turn_lock(session):
                return get_runner().run_turn(session, req.message)

        try:
            result = await run_in_threadpool(_turn)
        except Exception as exc:  # noqa: BLE001 - mapped to a safe message below
            log.exception("chat turn failed (session %s): %s", session.session_id, type(exc).__name__)
            status, message = _map_runner_error(exc)
            return JSONResponse(
                {"error": message, "session_id": session.session_id}, status_code=status
            )
        return ChatResponse(
            session_id=session.session_id,
            reply=result.reply,
            order_list=result.order_list,
            cache_timestamp=result.cache_timestamp,
        )

    return app


_app: FastAPI | None = None
_app_lock = threading.Lock()


def __getattr__(name: str) -> Any:
    """Lazy module-level ``app`` for ``uvicorn brewchat.web.app:app``."""
    global _app
    if name == "app":
        with _app_lock:
            if _app is None:
                _app = create_app()
            return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
