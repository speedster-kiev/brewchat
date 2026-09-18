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
