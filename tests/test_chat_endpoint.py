"""POST /chat contract, session handling, and error mapping. The runner is a fake;
the Anthropic client is never constructed."""

from __future__ import annotations

import threading

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from brewchat.agent.models import OrderLine, OrderList
from brewchat.agent.runner import TurnResult
from brewchat.agent.session import Session
from brewchat.catalog.store import CacheMissingError
from brewchat.config import Settings
from brewchat.web.app import create_app
from brewchat.web.sessions import SessionStore

CACHE_TS = "2026-09-17T06:00:00+00:00"

ORDER = OrderList(
    items=[
        OrderLine(
            ingredient_name="Citra",
            ingredient_amount=50,
            ingredient_unit="g",
            source="matched",
            product_title="Citra Pellets 100 g",
            product_handle="citra-100g",
            url="https://hopcellar.example/products/citra-100g",
            quantity=1,
            unit_price=65.0,
            line_total=65.0,
            in_stock=True,
        )
    ],
    total=65.0,
    currency="DKK",
    vat_note="Prices shown include VAT.",
    cache_timestamp=CACHE_TS,
    handoff_note="There's no cart link: add these items at HopCellar yourself.",
    text="Citra Pellets 100 g x1 65.00 DKK",
)


class FakeRunner:
    """Exposes run_turn like the real Runner; records history in the session."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def run_turn(self, session: Session, message: str) -> TurnResult:
        self.calls.append((session.session_id, message))
        if self.error is not None:
            raise self.error
        session.messages.append({"role": "user", "content": message})
        n_user = sum(1 for m in session.messages if m["role"] == "user")
        reply = f"turn {n_user}"
        session.messages.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
        order = None
        if "recipe" in message:
            session.order_list = ORDER
            order = ORDER
        return TurnResult(reply=reply, order_list=order, tool_calls=[], cache_timestamp=CACHE_TS)


def _authed(settings: Settings, runner: FakeRunner) -> TestClient:
    client = TestClient(create_app(settings, runner=runner))
    r = client.post("/login", data={"passphrase": "test-passphrase"}, follow_redirects=False)
    assert r.status_code == 303
    return client


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(settings: Settings, runner: FakeRunner) -> TestClient:
    return _authed(settings, runner)


def test_chat_response_shape(client: TestClient) -> None:
    r = client.post("/chat", json={"message": "here is my recipe"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"session_id", "reply", "order_list", "cache_timestamp"}
    assert body["session_id"]
    assert body["reply"] == "turn 1"
    assert body["cache_timestamp"] == CACHE_TS
    ol = OrderList.model_validate(body["order_list"])
    assert ol.total == 65.0 and ol.items[0].url and ol.text


def test_order_list_null_when_not_built(client: TestClient) -> None:
    body = client.post("/chat", json={"message": "hello"}).json()
    assert body["order_list"] is None
    assert body["cache_timestamp"] == CACHE_TS


def test_same_session_id_shares_history(client: TestClient, runner: FakeRunner) -> None:
    first = client.post("/chat", json={"message": "hello"}).json()
    sid = first["session_id"]
    second = client.post("/chat", json={"session_id": sid, "message": "again"}).json()
    assert second["session_id"] == sid
    assert second["reply"] == "turn 2"
    assert runner.calls[0][0] == runner.calls[1][0] == sid


def test_unknown_session_id_starts_new_session(client: TestClient) -> None:
    first = client.post("/chat", json={"message": "hello"}).json()
    other = client.post("/chat", json={"session_id": "not-a-real-id", "message": "hi"}).json()
    assert other["session_id"] not in (first["session_id"], "not-a-real-id")
    assert other["reply"] == "turn 1"


def test_empty_message_rejected(client: TestClient, runner: FakeRunner) -> None:
    assert client.post("/chat", json={"message": ""}).status_code == 422
    assert runner.calls == []


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://secret-upstream.invalid/v1/messages")


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (anthropic.APIConnectionError(request=_request()), 502),
        (anthropic.APITimeoutError(request=_request()), 504),
        (
            anthropic.RateLimitError(
                "rate limited at https://secret-upstream.invalid",
                response=httpx2.Response(429, request=_request()),
                body=None,
            ),
            503,
        ),
        (
            anthropic.InternalServerError(
                "boom", response=httpx2.Response(500, request=_request()), body=None
            ),
            502,
        ),
        (CacheMissingError("Catalog cache not found at /secret/path.sqlite"), 503),
        (RuntimeError("Traceback ... https://hopcellar.example/json/products/all"), 500),
    ],
)
def test_runner_errors_become_safe_json(
    settings: Settings, error: Exception, status: int
) -> None:
    client = _authed(settings, FakeRunner(error=error))
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == status
    body = r.json()
    assert set(body) == {"error", "session_id"}
    text = r.text.lower()
    for leak in ("secret-upstream", "hopcellar.example", "/secret/path", "traceback", settings.supplier.base_url):
        assert leak.lower() not in text


def test_missing_cache_message_is_clear(settings: Settings) -> None:
    client = _authed(settings, FakeRunner(error=CacheMissingError("x")))
    assert "catalog" in client.post("/chat", json={"message": "hi"}).json()["error"].lower()


# -- SessionStore ---------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_store_get_or_create_and_reuse() -> None:
    store = SessionStore()
    s = store.get_or_create(None)
    assert store.get_or_create(s.session_id) is s
    assert store.get_or_create("unknown").session_id != "unknown"
    assert len(store) == 2


def test_store_idle_expiry() -> None:
    clock = Clock()
    store = SessionStore(clock=clock)
    old = store.get_or_create(None)
    clock.t += 30 * 60
    fresh = store.get_or_create(None)
    clock.t += 31 * 60  # old: 61 min idle, fresh: 31 min
    assert store.sweep() == 1
    assert old.session_id not in store and fresh.session_id in store
    assert store.get_or_create(old.session_id) is not old


def test_store_thread_safe() -> None:
    store = SessionStore()
    ids: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        for _ in range(200):
            s = store.get_or_create(None)
            with lock:
                ids.append(s.session_id)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == len(ids) == len(store) == 1600


def test_missing_api_key_is_reported_clearly():
    from brewchat.web.app import _map_runner_error

    exc = TypeError('"Could not resolve authentication method. Expected one of api_key..."')
    status, message = _map_runner_error(exc)
    assert status == 503
    assert "ANTHROPIC_API_KEY" in message
