"""Access gate: login, signed cookie, robots handling. The runner is a fake."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from brewchat.config import ConfigError, Settings
from brewchat.web.app import create_app
from brewchat.web.auth import COOKIE_NAME, Gate, signing_secret
from tests.test_chat_endpoint import FakeRunner


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, runner=FakeRunner()))


def _login(client: TestClient, passphrase: str = "test-passphrase"):
    return client.post("/login", data={"passphrase": passphrase}, follow_redirects=False)


def test_unauthenticated_get_root_redirects_to_login(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/login"
    assert r.text == ""


def test_unauthenticated_chat_is_401_with_only_a_login_hint(client: TestClient) -> None:
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 401
    assert r.json() == {"error": "Login required.", "login": "/login"}


def test_unauthenticated_other_paths_are_gated(client: TestClient) -> None:
    assert client.get("/static/index.html", follow_redirects=False).status_code == 303
    assert client.get("/docs", follow_redirects=False).status_code == 303
    assert client.post("/whatever").status_code == 401


def test_login_page_is_public_and_only_a_form(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="passphrase"' in r.text
    assert "HopCellar" not in r.text and "hopcellar" not in r.text


def test_wrong_passphrase_rejected(client: TestClient) -> None:
    r = _login(client, "nope")
    assert r.status_code == 401
    assert COOKIE_NAME not in r.cookies
    assert "Wrong passphrase" in r.text
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_right_passphrase_sets_cookie_and_unlocks(client: TestClient) -> None:
    r = _login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    set_cookie = r.headers["set-cookie"]
    assert COOKIE_NAME in set_cookie and "httponly" in set_cookie.lower()
    assert client.post("/chat", json={"message": "hi"}).status_code == 200
    page = client.get("/")
    assert page.status_code == 200 and "<textarea" in page.text


def test_forged_cookie_rejected(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "ok")
    assert client.post("/chat", json={"message": "hi"}).status_code == 401
    other = Gate("some-other-passphrase").make_cookie()
    client.cookies.set(COOKIE_NAME, other)
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_logout_clears_access(client: TestClient) -> None:
    _login(client)
    client.post("/logout", follow_redirects=False)
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_robots_txt_disallows_all(client: TestClient) -> None:
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.text.splitlines()[:2] == ["User-agent: *", "Disallow: /"]


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/robots.txt"), ("get", "/login"), ("get", "/"), ("post", "/chat"), ("get", "/nope")],
)
def test_x_robots_tag_on_every_response(client: TestClient, method: str, path: str) -> None:
    r = getattr(client, method)(path, follow_redirects=False)
    assert "noindex" in r.headers["x-robots-tag"]
    _login(client)
    r = getattr(client, method)(path, follow_redirects=False)
    assert "noindex" in r.headers["x-robots-tag"]


def test_refuses_to_start_without_passphrase(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        create_app(dataclasses.replace(settings, passphrase=None), runner=FakeRunner())


def test_refuses_to_start_without_local_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("BREWCHAT_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigError):
        create_app(runner=FakeRunner())


def test_secret_from_env_or_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BREWCHAT_SECRET_KEY", raising=False)
    derived = signing_secret("pw")
    assert derived != "pw" and derived == signing_secret("pw") != signing_secret("pw2")
    monkeypatch.setenv("BREWCHAT_SECRET_KEY", "explicit")
    assert signing_secret("pw") == "explicit"


def test_importing_app_module_needs_no_config() -> None:
    import brewchat.web.app as mod

    assert callable(mod.create_app)
    with pytest.raises(AttributeError):
        _ = mod.not_a_thing
