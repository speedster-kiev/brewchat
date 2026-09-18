"""Passphrase gate: login form, signed cookie, and the middleware that enforces it.

Everything except ``/login`` and ``/robots.txt`` requires the cookie. Every
response carries ``X-Robots-Tag: noindex, nofollow``.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE_NAME = "brewchat_auth"
COOKIE_MAX_AGE = 14 * 24 * 3600
PUBLIC_PATHS = frozenset({"/login", "/robots.txt"})
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
ROBOTS_HEADER = "noindex, nofollow"
_SALT = "brewchat-auth"
_TOKEN = "ok"


def signing_secret(passphrase: str) -> str:
    """``$BREWCHAT_SECRET_KEY`` if set, else derived from the passphrase.

    Deriving from the passphrase means changing it logs everyone out.
    """
    explicit = os.environ.get("BREWCHAT_SECRET_KEY")
    if explicit:
        return explicit
    return hmac.new(b"brewchat-cookie-secret-v1", passphrase.encode(), hashlib.sha256).hexdigest()


class Gate:
    def __init__(self, passphrase: str) -> None:
        if not passphrase:
            raise ValueError("passphrase must be non-empty")
        self._passphrase = passphrase.encode()
        self._serializer = URLSafeTimedSerializer(signing_secret(passphrase), salt=_SALT)

    def check_passphrase(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._passphrase)

    def make_cookie(self) -> str:
        return self._serializer.dumps(_TOKEN)

    def is_authenticated(self, request: Request) -> bool:
        value = request.cookies.get(COOKIE_NAME)
        if not value:
            return False
        try:
            return self._serializer.loads(value, max_age=COOKIE_MAX_AGE) == _TOKEN
        except BadSignature:
            return False


def _login_page(error: str | None = None, status_code: int = 200) -> HTMLResponse:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>BrewChat login</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f5f0; --fg:#1f1d1a; --card:#fff; --line:#ddd6c8; --accent:#9a5b13; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16150f; --fg:#ece8df; --card:#211f19; --line:#3a362c; --accent:#e0a24e; }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
  font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
form {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:24px; width:min(340px, calc(100vw - 32px)); box-sizing:border-box; }}
h1 {{ margin:0 0 4px; font-size:1.3rem; }} p {{ margin:0 0 16px; opacity:.8; }}
input {{ width:100%; box-sizing:border-box; padding:10px 12px; font:inherit; border:1px solid var(--line); border-radius:8px; background:transparent; color:inherit; }}
button {{ margin-top:12px; width:100%; padding:10px; font:inherit; font-weight:600; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }}
.err {{ color:#c0392b; opacity:1; }}
</style></head>
<body><form method="post" action="/login">
<h1>BrewChat</h1><p>Private demo. Enter the passphrase to continue.</p>{err}
<label for="p" hidden>Passphrase</label>
<input id="p" name="passphrase" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Log in</button>
</form></body></html>"""
    return HTMLResponse(body, status_code=status_code)


def install_auth(app: FastAPI, passphrase: str) -> Gate:
    gate = Gate(passphrase)
    router = APIRouter()

    @router.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(request: Request) -> HTMLResponse:
        return _login_page()

    @router.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        raw = (await request.body()).decode("utf-8", errors="replace")
        candidate = (parse_qs(raw).get("passphrase") or [""])[0]
        if not gate.check_passphrase(candidate):
            return _login_page("Wrong passphrase.", status_code=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE_NAME,
            gate.make_cookie(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return resp

    @router.post("/logout", include_in_schema=False)
    async def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def robots() -> PlainTextResponse:
        return PlainTextResponse(ROBOTS_TXT)

    app.include_router(router)

    @app.middleware("http")
    async def gate_middleware(request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or gate.is_authenticated(request):
            response = await call_next(request)
        elif request.method in ("GET", "HEAD"):
            response = RedirectResponse("/login", status_code=303)
        else:
            response = JSONResponse(
                {"error": "Login required.", "login": "/login"}, status_code=401
            )
        response.headers["X-Robots-Tag"] = ROBOTS_HEADER
        return response

    return gate
