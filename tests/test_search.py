from datetime import UTC, datetime

import pytest

from brewchat.catalog.search import CatalogIndex, normalize
from brewchat.catalog.sync import filter_and_project, write_cache
from tests.conftest import SAMPLE_FETCHED_AT

CANDIDATE_KEYS = {"handle", "title", "score", "in_stock", "stock", "price", "ingredient_type"}


@pytest.fixture
def index(settings, catalog_cache) -> CatalogIndex:
    return CatalogIndex(catalog_cache, include_vat=True)


def test_normalize_strips_packaging_noise():
    assert normalize("Fermentis - SafAle US-05, 11,5 g. dry yeast") == "fermentis - safale us-05, dry yeast"
    assert normalize("Centennial hop pellets, 100 g, alpha 9,5%") == "centennial hop pellets, alpha 9,5%"
    assert normalize("Pilsner Malt, ebc 3 - 5 EBC, 1 kg") == "pilsner malt, ebc 3 - 5 ebc"


def test_us05_yeast_first(index):
    results = index.search("US-05", ingredient_type="yeast")
    assert results
    assert results[0]["handle"].startswith("/shop/8-gaer/301-")
    assert "US-05" in results[0]["title"]
    assert set(results[0]) == CANDIDATE_KEYS
    assert results[0]["price"] == 39.75
    assert all(r["ingredient_type"] == "yeast" for r in results)


def test_pilsner_malt_fermentable_never_returns_hop(index):
    results = index.search("Pilsner malt", ingredient_type="fermentable", limit=20)
    assert results
    assert results[0]["title"].startswith("Pilsner Malt")
    assert all(r["ingredient_type"] == "fermentable" for r in results)
    assert not any("hop" in r["title"].lower() for r in results)


def test_nonsense_query_returns_empty(index):
    assert index.search("xqzvbnm") == []
    assert index.search("") == []


def test_out_of_stock_returned_and_flagged(index):
    galaxy = index.search("Galaxy", ingredient_type="hop")
    assert galaxy[0]["title"].startswith("Galaxy")
    assert galaxy[0]["in_stock"] is False
    amarillo = index.search("Amarillo", ingredient_type="hop")
    assert amarillo[0]["title"].startswith("Amarillo")
    assert amarillo[0]["in_stock"] is False
    citra = index.search("Citra", ingredient_type="hop")
    assert citra[0]["in_stock"] is True


def test_limit_respected_and_scores_descending(index):
    for limit in (1, 3, 5):
        results = index.search("hop pellets", ingredient_type="hop", limit=limit)
        assert len(results) == limit
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)
    results = index.search("malt", limit=10)
    assert len(results) <= 10
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_price_follows_vat_flag(catalog_cache):
    ex = CatalogIndex(catalog_cache, include_vat=False)
    assert ex.search("US-05", ingredient_type="yeast")[0]["price"] == 31.8


def test_get_by_handle_and_cache_timestamp(index):
    p = index.get("/shop/286-pilsner-malt/102-pilsner-malt-ebc-3-5-ebc-1-kg/")
    assert p is not None and p.id == 102
    assert index.get("shop/286-pilsner-malt/102-pilsner-malt-ebc-3-5-ebc-1-kg") is p
    assert index.get("/shop/nope/") is None
    assert index.get("") is None
    assert index.cache_timestamp == SAMPLE_FETCHED_AT


def test_reloads_when_fetched_at_changes(settings, sample_raw, catalog_cache, index):
    assert index.search("Citra", ingredient_type="hop")[0]["in_stock"] is True
    for p in sample_raw["products"]:
        if p["Id"] == 205:
            p["Soldout"] = True
            p["StockWithoutReservation"] = 0
    later = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
    write_cache(filter_and_project(sample_raw, settings), catalog_cache, later)
    assert index.search("Citra", ingredient_type="hop")[0]["in_stock"] is False
    assert index.cache_timestamp == later
