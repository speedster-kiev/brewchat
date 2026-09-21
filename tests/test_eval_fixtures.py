"""Offline checks for the eval fixtures and the scoring/verdict logic (no API, no network)."""

import copy
import os
import subprocess
import sys

import pytest

from brewchat import evals
from brewchat.agent.models import Ingredient

# -- recipes and expected parses ---------------------------------------------


def test_recipes_md_has_ten_nonempty_recipes():
    recipes = evals.load_recipes()
    assert sorted(recipes) == list(range(1, 11))
    for n, text in recipes.items():
        assert len(text.strip().splitlines()) >= 5, f"recipe {n} looks empty"


def test_expected_has_every_recipe_and_valid_ingredients():
    expected = evals.load_expected()
    assert set(expected) == {f"recipe_{n}" for n in range(1, 11)}
    for rid, entries in expected.items():
        assert entries, f"{rid} has no ingredients"
        for e in entries:
            Ingredient.model_validate(
                e
            )  # extra scoring keys (aliases, ...) are ignored
            assert e["amount"] > 0, f"{rid}: {e['name']}"
            for t in e.get("accept_types", []):
                assert t in ("fermentable", "hop", "yeast", "other")
            for amt, unit in e.get("alt_amounts", []):
                assert amt > 0 and isinstance(unit, str)


def test_expected_units_are_all_known():
    for rid, entries in evals.load_expected().items():
        for e in entries:
            family, _ = evals.normalize_unit(e["amount"], e["unit"])
            assert not family.startswith("raw:"), (
                f"{rid}: unknown unit {e['unit']!r} on {e['name']}"
            )


def test_expected_excludes_non_ingredients():
    expected = evals.load_expected()
    r1 = " ".join(e["name"].lower() for e in expected["recipe_1"])
    assert "rye" not in r1  # the Red Rye footnote is not an ingredient
    r2 = expected["recipe_2"]
    assert (
        sum(e["type"] == "yeast" for e in r2) == 1
    )  # yeast description lines are not ingredients


# -- adversarial fixtures -------------------------------------------------------


def test_adversarial_parser_finds_all_fixtures():
    fixtures = evals.load_adversarial()
    by_id = {f.id: f for f in fixtures}
    expected_ids = (
        {f"A{i}" for i in range(1, 6)}
        | {f"B{i}" for i in range(1, 5)}
        | {f"C{i}" for i in range(1, 5)}
        | {f"D{i}" for i in range(1, 7)}
    )
    assert set(by_id) == expected_ids
    for fid, fx in by_id.items():
        assert fx.category == fid[0]
        assert fx.user_messages and all(m.strip() for m in fx.user_messages), fid
        assert fx.expected.strip(), fid


def test_b_fixtures_have_three_turns():
    for fx in evals.load_adversarial():
        if fx.category == "B":
            assert len(fx.turns) == 3, fx.id
            assert len(fx.user_messages) == 2, fx.id
            assert fx.off_topic_user_turns == [1]


# -- units and names -------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ((1, "kg"), (1000, "g")),
        ((1, "lb"), (453.6, "g")),
        ((8, "oz"), (226.8, "g")),
        ((1, "tsp"), (4.93, "ml")),
        ((2, "pk"), (2, "packs")),
        ((1, "smack pack"), (1, "pack")),
    ],
)
def test_unit_normalisation(a, b):
    fa, va = evals.normalize_unit(*a)
    fb, vb = evals.normalize_unit(*b)
    assert fa == fb
    assert evals.amounts_equal(va, vb)


def test_mass_and_count_are_different_families():
    assert evals.normalize_unit(11, "g")[0] != evals.normalize_unit(1, "sachet")[0]


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Crystal (60L) malt", "Crystal malt 60L", True),
        ("Wyeast 3068", "Wyeast 3068 Weihenstephan Weizen", True),
        ("Chocolate malt", "American Chocolate Malt", True),
        ("US-05", "Safale US05", True),
        ("Caramel 40", "Caramel 120", False),
        ("Flaked oats", "Flaked wheat", False),
        ("Pale malt", "Pilsner malt", False),
    ],
)
def test_name_similarity(a, b, same):
    assert (evals.name_similarity(a, b) >= evals.NAME_MATCH_THRESHOLD) is same


# -- scorer ---------------------------------------------------------------------


@pytest.mark.parametrize("rid", [f"recipe_{n}" for n in range(1, 11)])
def test_scorer_is_perfect_on_expected_vs_self(rid):
    expected = evals.load_expected()[rid]
    score = evals.score_recipe(rid, expected, copy.deepcopy(expected))
    assert score.accuracy == 1.0
    assert not score.missing and not score.extras and not score.mismatches


def _r1():
    return copy.deepcopy(evals.load_expected()["recipe_1"])


def test_scorer_penalises_wrong_amount():
    actual = _r1()
    actual[0]["amount"] *= 2
    score = evals.score_recipe("recipe_1", _r1(), actual)
    assert score.accuracy < 1.0
    assert score.field_correct["amount"] == score.field_total["amount"] - 1
    assert score.mismatches[0]["wrong_fields"] == ["amount"]


def test_scorer_penalises_wrong_type():
    actual = _r1()
    actual[0]["type"] = "other" if actual[0]["type"] != "other" else "hop"
    score = evals.score_recipe("recipe_1", _r1(), actual)
    assert score.accuracy < 1.0
    assert score.field_correct["type"] == score.field_total["type"] - 1


def test_scorer_accepts_equivalent_units():
    actual = _r1()
    for e in actual:
        if e["unit"] == "kg":
            e["amount"], e["unit"] = e["amount"] * 1000, "g"
    assert evals.score_recipe("recipe_1", _r1(), actual).accuracy == 1.0


def test_scorer_penalises_missing_and_extra():
    exp = _r1()
    missing = evals.score_recipe("recipe_1", exp, exp[1:])
    assert len(missing.missing) == 1 and missing.accuracy < 1.0
    extra = evals.score_recipe(
        "recipe_1",
        exp,
        [*_r1(), {"type": "other", "name": "Gypsum", "amount": 5, "unit": "g"}],
    )
    assert len(extra.extras) == 1 and extra.accuracy < 1.0


def test_extract_parsed_ingredients_takes_last_call():
    calls = [
        {"name": "submit_parsed_recipe", "input": {"ingredients": [{"name": "old"}]}},
        {"name": "search_catalog", "input": {"query": "x"}},
        {"name": "submit_parsed_recipe", "input": {"ingredients": [{"name": "new"}]}},
    ]
    assert evals.extract_parsed_ingredients(calls) == [{"name": "new"}]
    assert evals.extract_parsed_ingredients([]) is None


# -- adversarial verdicts ------------------------------------------------------------


def _fx(fid):
    return next(f for f in evals.load_adversarial() if f.id == fid)


def _turn(reply="", tool_calls=None, order_list=None):
    return {
        "message": "",
        "reply": reply,
        "tool_calls": tool_calls or [],
        "order_list": order_list,
    }


REDIRECT = "Sorry, I can only help with turning brewing recipes into an order list. Paste a recipe and I'll price it up."


def test_a_fixture_fails_on_any_tool_call():
    fx = _fx("A1")
    assert evals.evaluate_fixture(fx, [_turn(REDIRECT)]).passed
    bad = [_turn(REDIRECT, [{"name": "search_catalog", "input": {"query": "x"}}])]
    assert not evals.evaluate_fixture(fx, bad).passed


def test_b_fixture_only_checks_the_off_topic_turn():
    fx = _fx("B1")
    on_topic = _turn(
        "Here is your list",
        [{"name": "submit_parsed_recipe", "input": {"ingredients": []}}],
    )
    assert evals.evaluate_fixture(fx, [on_topic, _turn(REDIRECT)]).passed
    assert not evals.evaluate_fixture(
        fx, [on_topic, _turn(REDIRECT, [{"name": "search_catalog", "input": {}}])]
    ).passed


GALAXY_HANDLE = "/shop/54-australsk-humle/206-galaxy-hop-pellets-100-g-alpha-15-0/"
SUB_HANDLE = "/shop/51-amerikanske-humle-pellets/999-citra/"
PARSE_CALL = {
    "name": "submit_parsed_recipe",
    "input": {"ingredients": [{"name": "Galaxy"}]},
}


def _order(source, handle, in_stock):
    return {
        "items": [
            {
                "ingredient_name": "Galaxy",
                "source": source,
                "product_handle": handle,
                "product_title": "Galaxy hop pellets"
                if handle == GALAXY_HANDLE
                else "Citra",
                "in_stock": in_stock,
            }
        ]
    }


def test_c4_matched_sold_out_galaxy_fails_and_substitute_passes():
    fx = _fx("C4")
    sold_out = {GALAXY_HANDLE.strip("/")}
    matched = _turn(
        "All in stock!", [PARSE_CALL], _order("matched", GALAXY_HANDLE, False)
    )
    assert not evals.evaluate_fixture(fx, [matched], sold_out_handles=sold_out).passed
    subbed = _turn(
        "Galaxy is sold out; I suggest Citra.",
        [PARSE_CALL],
        _order("substituted", SUB_HANDLE, True),
    )
    assert evals.evaluate_fixture(fx, [subbed], sold_out_handles=sold_out).passed
    unavailable = _turn(
        "Galaxy is sold out.", [PARSE_CALL], _order("unavailable", None, None)
    )
    assert evals.evaluate_fixture(fx, [unavailable], sold_out_handles=sold_out).passed


def test_c3_config_leak_fails():
    fx = _fx("C3")
    assert evals.evaluate_fixture(
        fx, [_turn("Here is the parsed recipe.", [PARSE_CALL])]
    ).passed
    leak = _turn("The API is at https://hopcellar.example/json/products", [PARSE_CALL])
    assert not evals.evaluate_fixture(fx, [leak]).passed


def test_d_redirect_fails():
    fx = _fx("D1")
    assert not evals.evaluate_fixture(
        fx, [_turn(REDIRECT)] * len(fx.user_messages)
    ).passed
    answer = (
        "Good question. For a hazy IPA you want soft water, a high chloride to sulfate ratio, "
        "and plenty of oats and wheat; dry hop during active fermentation for biotransformation."
    )
    assert evals.evaluate_fixture(fx, [_turn(answer)] * len(fx.user_messages)).passed


def test_d5_origin_pick_needs_the_belgian_pilsner_as_a_match():
    fx = _fx("D5")
    assert "Belgian Pilsner malt" in fx.user_messages[0]
    castle = "shop/286-pilsner-malt/124-pilsner-malt-castle-malting-ebc-3-5-pr-100-g"
    danish = "shop/286-pilsner-malt/125-pilsner-malt-fuglsang-ebc-4-pr-100-g"

    def turn(source, handle, name="Belgian Pilsner malt"):
        return _turn("Here is your list.", [PARSE_CALL], {"items": [{
            "ingredient_name": name, "source": source, "in_stock": True,
            "product_title": "Pilsner Malt", "product_handle": f"/{handle}/",
        }]})

    assert evals.evaluate_fixture(fx, [turn("matched", castle)]).passed
    assert not evals.evaluate_fixture(fx, [turn("matched", danish)]).passed
    assert not evals.evaluate_fixture(fx, [turn("substituted", castle)]).passed
    assert not evals.evaluate_fixture(fx, [turn("matched", castle, name="Cara Blond")]).passed
    assert not evals.evaluate_fixture(fx, [_turn("Here is your list.")]).passed  # no list built


def test_judge_verdict_parsing():
    assert evals.parse_judge_verdict('{"verdict": "pass", "reason": "ok"}') == (
        True,
        "ok",
    )
    ok, _ = evals.parse_judge_verdict("garbage")
    assert ok is False


# -- CLI --------------------------------------------------------------------------------


def _run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, str(evals.REPO_ROOT / "scripts" / "run_evals.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_cli_help_works():
    proc = _run_cli("--help")
    assert proc.returncode == 0
    assert "parse" in proc.stdout and "adversarial" in proc.stdout


def test_cli_refuses_without_api_key():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }
    env["BREWCHAT_ENV_FILE"] = ""  # don't pick up a developer's local .env key
    proc = _run_cli("parse", env=env)
    assert proc.returncode == 2
    assert "ANTHROPIC_API_KEY" in proc.stderr


def test_cost_estimate_uses_cache_multipliers():
    sys.path.insert(0, str(evals.REPO_ROOT / "scripts"))
    import run_evals

    usage = {
        "input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    # Haiku 4.5: $1 in, $5 out -> 1 + 1.25 + 0.1 + 5
    assert run_evals.estimate_cost("claude-haiku-4-5", usage) == pytest.approx(7.35)
    assert run_evals.estimate_cost("claude-opus-5", {"output_tokens": 20_000}) == pytest.approx(0.5)
    assert run_evals.estimate_cost("some-unknown-model", usage) is None
    assert run_evals.sum_usage([{"output_tokens": 1}, {"output_tokens": 2, "api_calls": 1}]) == {
        "output_tokens": 3,
        "api_calls": 1,
    }


def test_d6_suggestion_must_be_a_match_and_the_reply_must_show_alternatives():
    fx = _fx("D6")
    assert "Munich Helles" in fx.user_messages[0]
    bestmalz = "shop/286-pilsner-malt/123-pilsner-malt-bestmalz-ebc-2-4-pr-100-g"
    danish = "shop/286-pilsner-malt/125-pilsner-malt-fuglsang-ebc-4-pr-100-g"

    def turn(source, handle, reply):
        return _turn(reply, [PARSE_CALL], {"items": [{
            "ingredient_name": "Pilsner malt", "source": source, "in_stock": True,
            "product_title": "Pilsner Malt", "product_handle": f"/{handle}/",
        }]})

    shows = "Here is your list. Also in stock: Castle Malting (BE), 1.75 DKK. Say if you'd rather have it."
    assert evals.evaluate_fixture(fx, [turn("matched", bestmalz, shows)]).passed
    assert not evals.evaluate_fixture(fx, [turn("matched", bestmalz, "Here is your list.")]).passed
    assert not evals.evaluate_fixture(fx, [turn("matched", danish, shows)]).passed
    assert not evals.evaluate_fixture(fx, [turn("user_override", bestmalz, shows)]).passed
