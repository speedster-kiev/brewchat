from pathlib import Path

import pytest

from brewchat.config import EXAMPLE_CONFIG, INGREDIENT_TYPES, ConfigError, load_settings


def test_missing_local_config_points_to_example(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BREWCHAT_CONFIG", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path / "supplier.local.toml")
    assert "config/supplier.example.toml" in str(exc.value)


def test_example_config_parses():
    s = load_settings(EXAMPLE_CONFIG)
    assert s.supplier.name == "HopCellar"
    assert s.supplier.base_url == "https://hopcellar.example"
    assert s.supplier.products_all_url == "https://hopcellar.example/json/products/all"
    assert s.cache_path.name == "catalog.sqlite"


def test_categories_map_to_the_four_ingredient_types_only():
    s = load_settings(EXAMPLE_CONFIG)
    assert s.categories
    assert set(s.categories.values()) <= set(INGREDIENT_TYPES)
    assert set(s.categories.values()) == set(INGREDIENT_TYPES)


def test_ingredient_type_secondary_fallback_and_exclusion():
    s = load_settings(EXAMPLE_CONFIG)
    assert s.ingredient_type_for(8, []) == "yeast"
    assert s.ingredient_type_for(121, [6]) == "fermentable"  # promo category, malt secondary
    assert s.ingredient_type_for(137, [8]) is None  # distillation yeast excluded
    assert s.ingredient_type_for(76, []) is None  # equipment


def test_unknown_ingredient_type_rejected(tmp_path: Path):
    text = EXAMPLE_CONFIG.read_text().replace('"6" = "fermentable"', '"6" = "grain"')
    bad = tmp_path / "bad.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_vat_note_follows_flag():
    s = load_settings(EXAMPLE_CONFIG)
    assert "include VAT" in s.supplier.vat_note


def test_env_file_loaded_without_overriding_real_env(tmp_path: Path, monkeypatch):
    from brewchat.config import load_env_file

    env = tmp_path / ".env"
    env.write_text("BREWCHAT_TEST_A=from-file\nBREWCHAT_TEST_B=from-file\n")
    monkeypatch.setenv("BREWCHAT_ENV_FILE", str(env))
    monkeypatch.delenv("BREWCHAT_TEST_A", raising=False)
    monkeypatch.setenv("BREWCHAT_TEST_B", "from-env")
    assert load_env_file() == env
    import os

    assert os.environ["BREWCHAT_TEST_A"] == "from-file"
    assert os.environ["BREWCHAT_TEST_B"] == "from-env"
    monkeypatch.delenv("BREWCHAT_TEST_A")


def test_env_file_can_be_disabled(monkeypatch):
    from brewchat.config import load_env_file

    monkeypatch.setenv("BREWCHAT_ENV_FILE", "")
    assert load_env_file() is None


def test_model_defaults_and_env_override(monkeypatch):
    from brewchat.config import DEFAULT_MODEL

    monkeypatch.delenv("BREWCHAT_MODEL", raising=False)
    assert load_settings(EXAMPLE_CONFIG).model == DEFAULT_MODEL
    monkeypatch.setenv("BREWCHAT_MODEL", "claude-sonnet-5")
    assert load_settings(EXAMPLE_CONFIG).model == "claude-sonnet-5"
