import json

import pytest
from anthropic.lib.tools import ToolError
from pydantic import ValidationError

from brewchat.agent.models import OrderListItem
from brewchat.agent.session import Session, current_session
from brewchat.agent.tools import HANDOFF_NOTE, ToolContext, make_tools

PILS = "/shop/286-pilsner-malt/102-pilsner-malt-ebc-3-5-ebc-1-kg/"
CITRA = "/shop/51-amerikanske-humle-pellets/205-citra-hop-pellets-100-g-alpha-12-5/"


@pytest.fixture
def ctx(settings, catalog_cache) -> ToolContext:
    return ToolContext.from_settings(settings)


@pytest.fixture
def tools(ctx) -> dict:
    return {t.name: t for t in make_tools(ctx)}


@pytest.fixture
def session():
    s = Session()
    token = current_session.set(s)
    yield s
    current_session.reset(token)


def test_tool_names_and_schemas(tools):
    assert set(tools) == {"submit_parsed_recipe", "search_catalog", "build_order_list"}
    for t in tools.values():
        d = t.to_dict()
        assert d["description"]
        assert d["input_schema"]["type"] == "object"
    schema = json.dumps(tools["build_order_list"].to_dict()["input_schema"])
    assert "OrderListItem" in schema and "Substitution" in schema
    assert "out-of-stock" in tools["search_catalog"].description.lower()


def test_submit_parsed_recipe_stores_parse(tools, session):
    out = tools["submit_parsed_recipe"].call({"ingredients": [
        {"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"},
        {"type": "hop", "name": "Saaz", "amount": 30, "unit": "g", "timing": "60 min", "spec": "3.5% AA"},
    ]})
    assert "2" in out
    assert [i.name for i in session.parsed_recipe] == ["Pilsner malt", "Saaz"]


def test_submit_parsed_recipe_records_staples_but_only_asks_for_the_rest(tools, session):
    # Issue #14: the parse keeps the staple (parse eval), the model is only told what to source.
    out = tools["submit_parsed_recipe"].call({"ingredients": [
        {"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"},
        {"type": "other", "name": "Cane sugar", "amount": 500, "unit": "g"},
        {"type": "other", "name": "Belgian candi sugar", "amount": 500, "unit": "g"},
        {"type": "hop", "name": "Saaz", "amount": 30, "unit": "g", "timing": "60 min"},
        {"type": "hop", "name": "Saaz", "amount": 20, "unit": "g", "timing": "5 min"},
    ]})
    assert [i.name for i in session.parsed_recipe] == [
        "Pilsner malt", "Cane sugar", "Belgian candi sugar", "Saaz", "Saaz"]
    assert out.endswith("search the catalog for each of these and list them: Pilsner malt; Belgian candi sugar; Saaz.")
    assert "cane" not in out.lower() and "staple" not in out.lower()


def test_submit_parsed_recipe_with_only_staples_asks_for_nothing(tools, session):
    out = tools["submit_parsed_recipe"].call({"ingredients": [
        {"type": "other", "name": "Table sugar", "amount": 1, "unit": "kg"}]})
    assert "Nothing to search for" in out and "Table sugar" not in out
    assert [i.name for i in session.parsed_recipe] == ["Table sugar"]


@pytest.mark.parametrize("query", ["cane sugar", "Sugar", "white sugar (granulated)"])
def test_search_catalog_never_searches_a_staple(tools, session, ctx, monkeypatch, query):
    def boom(*args, **kwargs):
        raise AssertionError("the catalog was searched for a staple")

    monkeypatch.setattr(ctx.index, "search", boom)
    out = tools["search_catalog"].call({"query": query, "ingredient_type": "other"})
    assert "pantry staple" in out and "candidates" not in out


def test_search_catalog_still_searches_brewing_sugars(tools, session):
    out = json.loads(tools["search_catalog"].call({"query": "candi sugar", "ingredient_type": "other"}))
    assert out["candidates"][0]["title"].startswith("Candi sugar")


def test_submit_parsed_recipe_stores_style(tools, session):
    ingredients = [{"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"}]
    tools["submit_parsed_recipe"].call({"ingredients": ingredients, "style": " Munich Helles "})
    assert session.style == "Munich Helles"
    tools["submit_parsed_recipe"].call({"ingredients": ingredients})  # a corrected recipe without one
    assert session.style is None


def test_search_catalog_uses_the_recipe_style_unless_told_otherwise(tools, session):
    ingredients = [{"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"}]

    def top(**extra):
        out = json.loads(tools["search_catalog"].call(
            {"query": "Pilsner malt", "ingredient_type": "fermentable", **extra}))
        return out["candidates"][0]

    assert "no origin or style" in top()["why"]
    tools["submit_parsed_recipe"].call({"ingredients": ingredients, "style": "Munich Helles"})
    assert top()["origin"] == "DE" and "Munich Helles" in top()["why"]
    assert top(style="Belgian Tripel")["origin"] == "BE"


def test_submit_parsed_recipe_rejects_bad_type(tools, session):
    with pytest.raises(ToolError) as exc:
        tools["submit_parsed_recipe"].call({"ingredients": [
            {"type": "grain", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"},
        ]})
    assert "ingredients.0.type" in str(exc.value)
    assert session.parsed_recipe is None


def test_search_catalog_returns_json_candidates(tools, session, settings):
    out = tools["search_catalog"].call({"query": "US-05", "ingredient_type": "yeast"})
    data = json.loads(out)
    assert "US-05" in data["candidates"][0]["title"]
    assert settings.supplier.base_url not in out


def test_search_catalog_no_match_message(tools, session):
    out = tools["search_catalog"].call({"query": "xqzvbnm"})
    assert out.startswith("No match")


def test_build_order_list_rejects_substituted_without_substitution(tools, session):
    bad = {
        "ingredient": {"type": "hop", "name": "Galaxy", "amount": 50, "unit": "g"},
        "product_handle": CITRA, "quantity": 1, "source": "substituted",
    }
    with pytest.raises(ValidationError):
        OrderListItem.model_validate(bad)
    with pytest.raises(ToolError, match="requires a substitution"):
        tools["build_order_list"].call({"items": [bad]})
    assert session.order_list is None


def test_build_order_list_tool_returns_text_and_stores_order(tools, session):
    out = tools["build_order_list"].call({"items": [{
        "ingredient": {"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"},
        "product_handle": PILS, "quantity": 5, "source": "matched",
    }]})
    assert out.rstrip().endswith(HANDOFF_NOTE)
    assert session.order_list is not None and session.order_list.text == out
    assert session.order_list.total == 100.0


def test_build_order_list_unknown_handle_is_tool_error(tools, session):
    with pytest.raises(ToolError, match="search_catalog"):
        tools["build_order_list"].call({"items": [{
            "ingredient": {"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"},
            "product_handle": "/shop/made-up/", "quantity": 5, "source": "matched",
        }]})


def test_tool_without_session_is_tool_error(tools):
    with pytest.raises(ToolError, match="session"):
        tools["submit_parsed_recipe"].call({"ingredients": [
            {"type": "hop", "name": "Saaz", "amount": 30, "unit": "g"},
        ]})


def test_build_order_list_reports_recalculated_quantities(tools, session):
    out = tools["build_order_list"].call({"items": [{
        "ingredient": {"type": "fermentable", "name": "Maris Otter", "amount": 5, "unit": "kg"},
        "product_handle": "/shop/6-malt/121-maris-otter-malt-northfield-maltings-ebc-5-7-pr-100-g/",
        "quantity": 1, "source": "matched",
    }]})
    assert out.startswith("Quantities recalculated from pack sizes")
    assert "- Maris Otter: quantity 1 -> 50 (5 kg needed, sold per 100 g)" in out
    assert out.endswith(session.order_list.text)


def test_search_candidates_carry_pack_size(tools, session):
    out = json.loads(tools["search_catalog"].call({"query": "Maris Otter", "ingredient_type": "fermentable"}))
    sizes = {c["handle"].split("/")[3][:3]: c["pack_size"] for c in out["candidates"]}
    assert sizes["121"] == "100 g" and sizes["122"] == "25 kg"


def test_search_catalog_finds_sugars_filed_under_other_when_asked_for_fermentable(tools, session):
    out = json.loads(tools["search_catalog"].call({"query": "candi sugar", "ingredient_type": "fermentable"}))
    assert out["candidates"][0]["title"].startswith("Candi sugar")
    assert {c["ingredient_type"] for c in out["candidates"]} <= {"fermentable", "other"}


BESTMALZ = "/shop/286-pilsner-malt/123-pilsner-malt-bestmalz-ebc-2-4-pr-100-g/"
CASTLE = "/shop/286-pilsner-malt/124-pilsner-malt-castle-malting-ebc-3-5-pr-100-g/"
US05 = "/shop/8-gaer/301-fermentis-safale-us-05-11-5-g-dry-yeast/"
HELLES = [{"type": "fermentable", "name": "Pilsner malt", "amount": 4.5, "unit": "kg"}]


def _build_pils(tools, handle=BESTMALZ, source="matched", **extra):
    return tools["build_order_list"].call({"items": [{
        "ingredient": HELLES[0], "product_handle": handle, "quantity": 45, "source": source, **extra,
    }]})


def test_build_order_list_names_equally_good_alternatives_to_the_suggestion(tools, session):
    # D6 / issue #1: the alternatives come back with the built list, not only from a search
    # the model may have made a dozen tool calls earlier.
    tools["submit_parsed_recipe"].call({"ingredients": HELLES, "style": "Munich Helles"})
    tools["search_catalog"].call({"query": "Pilsner malt", "ingredient_type": "fermentable"})
    tools["search_catalog"].call({"query": "Munich malt", "ingredient_type": "fermentable"})
    out = _build_pils(tools)
    head, _, rest = out.partition("\n\n")
    assert head.startswith("Equally good in-stock alternatives")
    assert "say the user can switch" in head
    assert "- Pilsner malt (listed: Pilsner Malt - Bestmalz" in head
    assert "Pilsner Malt - Castle Malting, ebc 3 - 5, pr. 100 g. (BE, 2.50 DKK per 100 g)" in head
    assert "Pilsner Malt - Fuglsang, ebc 4, pr. 100 g. (DK, 2.50 DKK per 100 g)" in head
    assert "Pilsner Malt, ebc 3 - 5 EBC, 1 kg (origin unstated, 20.00 DKK per 1 kg)" in head
    assert "Bohemian Pilsner malt - Weyermann, ebc 3 - 5, pr. 25 kg. (DE, 525.00 DKK per 25 kg)" in head
    assert head.count("Bestmalz") == 1  # the listed product is not its own alternative
    assert rest == session.order_list.text  # the copyable list itself is unchanged


def test_build_order_list_has_no_alternatives_note_without_a_tie_or_for_a_user_choice(tools, session):
    tools["search_catalog"].call({"query": "US-05", "ingredient_type": "yeast"})
    out = tools["build_order_list"].call({"items": [{
        "ingredient": {"type": "yeast", "name": "US-05", "amount": 1, "unit": "pack"},
        "product_handle": US05, "quantity": 1, "source": "matched",
    }]})
    assert out == session.order_list.text

    tools["search_catalog"].call({"query": "Pilsner malt", "ingredient_type": "fermentable"})
    out = _build_pils(tools, CASTLE, "user_override", substitution={
        "original_name": "Pilsner malt", "reason": "the user's choice among equally good matches",
        "confidence": "high"})
    assert "Equally good" not in out


def test_submit_parsed_recipe_forgets_alternatives_from_an_earlier_recipe(tools, session):
    tools["search_catalog"].call({"query": "Pilsner malt", "ingredient_type": "fermentable"})
    assert BESTMALZ in session.alternatives
    tools["submit_parsed_recipe"].call({"ingredients": HELLES})
    assert session.alternatives == {}
