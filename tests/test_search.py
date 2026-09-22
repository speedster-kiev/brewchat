from datetime import UTC, datetime

import pytest

from brewchat.catalog.search import CatalogIndex, normalize
from brewchat.catalog.sync import filter_and_project, write_cache
from tests.conftest import SAMPLE_FETCHED_AT

CANDIDATE_KEYS = {
    "handle", "title", "score", "in_stock", "price", "pack_size", "ingredient_type", "origin",
    "suggested", "why",
}


@pytest.fixture
def index(settings, catalog_cache) -> CatalogIndex:
    return CatalogIndex(
        catalog_cache, include_vat=True, origins=settings.origins, style_table=settings.style_origins
    )


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
    def citra_100g() -> dict:
        hits = index.search("Citra", ingredient_type="hop", limit=20)
        return next(r for r in hits if r["handle"].split("/")[3].startswith("205-"))

    assert citra_100g()["in_stock"] is True
    for p in sample_raw["products"]:
        if p["Id"] == 205:
            p["Soldout"] = True
            p["StockWithoutReservation"] = 0
    later = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
    write_cache(filter_and_project(sample_raw, settings), catalog_cache, later)
    assert citra_100g()["in_stock"] is False
    assert index.cache_timestamp == later


# -- origin (issue #1) ----------------------------------------------------------


# The synthetic catalog has four pilsner malts (BE, DE, DK, none), so ask for four.
N_PILSNERS = 4


def _all_pilsner(results):
    return bool(results) and all("pilsner malt" in r["title"].lower() for r in results)


def test_belgian_query_returns_pilsner_malts_with_the_belgian_one_first(index):
    results = index.search("Belgian Pilsner malt", ingredient_type="fermentable", limit=N_PILSNERS)
    assert _all_pilsner(results)
    assert results[0]["title"].startswith("Pilsner Malt - Castle Malting")
    assert results[0]["origin"] == "BE" and results[0]["in_stock"] is True
    # Without the origin word the alphabetical tie-break decides, and it is not the Belgian one.
    assert index.search("Pilsner malt", ingredient_type="fermentable")[0]["origin"] != "BE"


@pytest.mark.parametrize(("query", "origin"), [("Danish pilsner malt", "DK"), ("German pilsner malt", "DE")])
def test_origin_word_prefers_that_maltster(index, query, origin):
    results = index.search(query, ingredient_type="fermentable", limit=N_PILSNERS)
    assert _all_pilsner(results)
    assert results[0]["origin"] == origin


def test_origin_only_breaks_ties_it_never_outranks_a_better_match(index):
    # A Belgian query for a malt the Belgian maltster doesn't make must not surface Castle Malting first.
    results = index.search("Belgian Munich malt", ingredient_type="fermentable")
    assert results[0]["title"].startswith("Munich Malt")
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_asked_for_origin_comes_before_stock_but_stays_flagged(settings, sample_raw, catalog_cache, index):
    for p in sample_raw["products"]:
        if p["Id"] == 124:
            p["StockWithoutReservation"] = 0
    write_cache(filter_and_project(sample_raw, settings), catalog_cache, datetime(2026, 9, 18, tzinfo=UTC))
    top = index.search("Belgian pilsner malt", ingredient_type="fermentable")[0]
    assert top["origin"] == "BE" and top["in_stock"] is False


def test_hop_origin_comes_from_the_title_token(index):
    citra = {r["handle"].split("/")[3].split("-")[0]: r for r in index.search("Citra", "hop", limit=20)}
    assert citra["221"]["origin"] == "US"  # "Citra hop pellets US, ..."
    assert citra["205"]["origin"] is None  # "Citra hop pellets, 100 g, ..." names no country


def test_unknown_producers_have_no_origin_and_rank_as_before(settings, catalog_cache):
    bare = CatalogIndex(catalog_cache, include_vat=True)  # no producer map at all
    results = bare.search("Pilsner malt", ingredient_type="fermentable", limit=20)
    assert {r["origin"] for r in results} == {None}
    assert [r["handle"] for r in results] == [
        r["handle"] for r in CatalogIndex(catalog_cache, include_vat=True, origins={}).search(
            "Pilsner malt", ingredient_type="fermentable", limit=20)
    ]
    # An origin word with nothing to prefer still finds the pilsner malts.
    assert _all_pilsner(bare.search("Belgian Pilsner malt", "fermentable", limit=N_PILSNERS))


def test_words_that_look_like_origins_do_not_disturb_other_searches(index):
    assert "US-05" in index.search("US-05", ingredient_type="yeast")[0]["title"]
    assert index.search("Irish moss")[0]["title"].startswith("Irish moss")


# -- beer style (rank and suggest, never exclude) ------------------------------


def test_style_ranks_its_usual_origin_first_and_drops_nothing(index):
    plain = index.search("Pilsner malt", ingredient_type="fermentable", limit=20)
    helles = index.search("Pilsner malt", ingredient_type="fermentable", limit=20, style="Munich Helles")
    assert {r["handle"] for r in helles} == {r["handle"] for r in plain}
    in_stock = [r for r in helles if r["in_stock"]]
    de_first = [r["origin"] == "DE" for r in in_stock]
    assert de_first[0] and de_first == sorted(de_first, reverse=True)  # every DE one before any other
    assert {r["origin"] for r in helles} >= {"DE", "BE", "DK"}  # the alternatives are still there


def test_style_never_outranks_a_better_match(index):
    results = index.search("Munich malt", ingredient_type="fermentable", style="Munich Helles")
    assert results[0]["title"].startswith("Munich Malt")
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_origin_named_in_the_query_beats_the_style(index):
    top = index.search("Belgian Pilsner malt", "fermentable", limit=N_PILSNERS, style="Munich Helles")[0]
    assert top["origin"] == "BE"


def test_unknown_style_changes_nothing(index):
    plain = index.search("Pilsner malt", ingredient_type="fermentable", limit=20)
    for style in (None, "", "Grätzer", "Cornelius Ale"):
        assert index.search("Pilsner malt", ingredient_type="fermentable", limit=20, style=style) == plain


def test_one_candidate_is_suggested_with_the_reason_it_won(index):
    results = index.search("Pilsner malt", ingredient_type="fermentable", limit=N_PILSNERS, style="Munich Helles")
    suggested = [r for r in results if r["suggested"]]
    assert suggested == [results[0]]
    assert results[0]["origin"] == "DE" and "DE" in results[0]["why"] and "Munich Helles" in results[0]["why"]
    assert all(r["why"] is None for r in results[1:])
    # No origin or style to go on: still suggests one, and says nothing decided it.
    plain = index.search("Pilsner malt", ingredient_type="fermentable", limit=N_PILSNERS)
    assert plain[0]["suggested"] and "no origin or style" in plain[0]["why"]


def test_suggestion_skips_an_out_of_stock_top_match(settings, sample_raw, catalog_cache, index):
    for p in sample_raw["products"]:
        if p["Id"] == 124:
            p["StockWithoutReservation"] = 0
    write_cache(filter_and_project(sample_raw, settings), catalog_cache, datetime(2026, 9, 18, tzinfo=UTC))
    results = index.search("Belgian pilsner malt", ingredient_type="fermentable", limit=N_PILSNERS)
    assert results[0]["origin"] == "BE" and results[0]["in_stock"] is False
    assert not results[0]["suggested"]
    picked = [r for r in results if r["suggested"]]
    assert len(picked) == 1 and picked[0]["in_stock"] is True


def test_no_suggestion_when_nothing_at_the_top_score_is_in_stock(settings, sample_raw, catalog_cache, index):
    for p in sample_raw["products"]:
        if p["Id"] == 124:
            p["StockWithoutReservation"] = 0
    write_cache(filter_and_project(sample_raw, settings), catalog_cache, datetime(2026, 9, 18, tzinfo=UTC))
    only = index.search("Pilsner Malt - Castle Malting", ingredient_type="fermentable", limit=1)
    assert only[0]["origin"] == "BE" and only[0]["in_stock"] is False
    assert only[0]["suggested"] is False
