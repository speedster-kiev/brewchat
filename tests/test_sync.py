import dataclasses
import json

import httpx
import pytest

from brewchat.catalog.schema import Product
from brewchat.catalog.store import cache_timestamp, load_ingredients
from brewchat.catalog.sync import SyncError, filter_and_project, sync, write_cache
from tests.conftest import SAMPLE_FETCHED_AT

PROJECTED_FIELDS = {f.name for f in dataclasses.fields(Product)}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_client(payload, status=200):
    return _client(lambda req: httpx.Response(status, json=payload))


def test_projection_keeps_exactly_schema_fields(settings, sample_raw):
    products = filter_and_project(sample_raw, settings)
    assert set(products[0].to_dict()) == PROJECTED_FIELDS
    assert "Link" not in products[0].to_dict()


def test_filter_keeps_only_mapped_categories(settings, sample_raw):
    products = filter_and_project(sample_raw, settings)
    ids = {p.id for p in products}
    assert len(products) == 46
    assert 501 in ids  # included via secondary category
    assert 601 not in ids  # excluded primary category
    assert not ids & {701, 702, 703, 704, 705}  # equipment and kits
    assert {p.ingredient_type for p in products} == {"fermentable", "hop", "yeast", "other"}


def _p(**kw) -> Product:
    base = dict(
        id=1, item_number=None, title="x", category_id=8, secondary_category_ids=[],
        category_title=None, stock=5, stock_without_reservation=5, soldout=False,
        buyable=True, online=False, price_with_vat=10.0, price_without_vat=8.0,
        handle="/shop/x/", ingredient_type="yeast",
    )
    return Product(**(base | kw))


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, True),
        ({"online": False}, True),  # Online is ignored
        ({"soldout": True}, False),
        ({"buyable": False}, False),
        ({"stock_without_reservation": 0}, False),
        ({"stock": 10, "stock_without_reservation": 0}, False),
    ],
)
def test_in_stock_truth_table(kw, expected):
    assert _p(**kw).in_stock() is expected


def test_sync_writes_cache_and_timestamp(settings, sample_raw):
    n = sync(settings, client=_json_client(sample_raw), now=SAMPLE_FETCHED_AT)
    assert n == 46
    assert len(load_ingredients(settings.cache_path)) == 46
    assert cache_timestamp(settings.cache_path) == SAMPLE_FETCHED_AT


def test_sync_sends_user_agent(settings, sample_raw):
    seen = {}

    def handler(req: httpx.Request):
        seen["ua"] = req.headers["user-agent"]
        seen["url"] = str(req.url)
        return httpx.Response(200, json=sample_raw)

    sync(settings, client=_client(handler))
    assert seen["ua"] == settings.supplier.user_agent
    assert seen["url"] == "https://hopcellar.example/json/products/all"


@pytest.mark.parametrize(
    "client_factory",
    [
        lambda raw: _json_client({"error": "boom"}, status=500),
        lambda raw: _json_client({"amount": 0, "items": []}),
        lambda raw: _json_client({"products": [p for p in raw["products"] if p["Id"] >= 700]}),
        lambda raw: _json_client({"products": [{"Id": 1, "CategoryId": 8}]}),
        lambda raw: _client(lambda req: httpx.Response(200, content=b"<html>not json</html>")),
    ],
    ids=["http-500", "no-products-key", "empty-after-filter", "shape-drift", "not-json"],
)
def test_failed_sync_leaves_previous_cache_byte_identical(settings, sample_raw, client_factory):
    write_cache(filter_and_project(sample_raw, settings), settings.cache_path, SAMPLE_FETCHED_AT)
    before = settings.cache_path.read_bytes()
    with pytest.raises(SyncError) as exc:
        sync(settings, client=client_factory(sample_raw))
    assert settings.cache_path.read_bytes() == before
    assert "hopcellar.example" not in str(exc.value)
    assert list(settings.cache_path.parent.iterdir()) == [settings.cache_path]


def test_raw_response_never_written(settings, sample_raw):
    sync(settings, client=_json_client(sample_raw))
    files = list(settings.cache_path.parent.iterdir())
    assert files == [settings.cache_path]
    assert b"DateCreated" not in settings.cache_path.read_bytes()
    assert json.dumps(sample_raw)  # sanity
