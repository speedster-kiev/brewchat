"""Shared fixtures: example config pointed at tmp paths, synthetic catalog, synced cache.

No network and no API key are used anywhere in the unit tests.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brewchat.catalog.sync import filter_and_project, write_cache
from brewchat.config import EXAMPLE_CONFIG, Settings, load_settings

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_FETCHED_AT = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


@pytest.fixture
def sample_raw() -> dict:
    return json.loads((FIXTURES / "catalog_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings from the committed example config, with cache and logs under tmp_path."""
    monkeypatch.delenv("BREWCHAT_LOGS_DIR", raising=False)
    monkeypatch.delenv("BREWCHAT_MODEL", raising=False)
    monkeypatch.setenv("BREWCHAT_PASSPHRASE", "test-passphrase")
    base = load_settings(EXAMPLE_CONFIG)
    return dataclasses.replace(
        base,
        cache_path=tmp_path / "data" / "catalog.sqlite",
        logs_dir=tmp_path / "logs",
    )


@pytest.fixture
def catalog_cache(settings: Settings, sample_raw: dict) -> Path:
    """A synced SQLite cache built from the synthetic catalog."""
    products = filter_and_project(sample_raw, settings)
    write_cache(products, settings.cache_path, SAMPLE_FETCHED_AT)
    return settings.cache_path
