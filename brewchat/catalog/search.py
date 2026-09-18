"""In-memory fuzzy search over the cached ingredient catalog (plan.md, step 3).

The index loads the SQLite cache once per process and reloads it only when
the cache's ``fetched_at`` changes (i.e. after a sync). Matching is a
category pre-filter by ingredient type followed by ``rapidfuzz`` WRatio over
lowercased titles with packaging noise ("11,5 g.", "1 kg", "100 g") removed.
The tool returns candidates; the agent, not the tool, picks the final match.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process, utils

from brewchat.catalog import store
from brewchat.catalog.schema import Product

SCORE_FLOOR = 40.0
MAX_LIMIT = 20

# "11,5 g.", "1 kg", "100 g", "25 kg", "500g", "30 L", "19 L", "205 g", "50 ml"
_PACKAGING = re.compile(
    r"(?<![\w.,])\d+(?:[.,]\d+)?\s*(?:kg|g|gr|mg|l|ltr|ml|cl)\b\.?",
    re.IGNORECASE,
)
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase and strip packaging sizes so they don't dominate the score."""
    out = _PACKAGING.sub(" ", text.lower())
    out = re.sub(r"\s+([,.;])", r"\1", out)
    out = re.sub(r"(?:[,;]\s*){2,}", ", ", out)
    return _SPACES.sub(" ", out).strip(" ,;.")


def _handle_key(handle: str) -> str:
    return "/" + handle.strip().strip("/") + "/"


class CatalogIndex:
    """Fuzzy search over the ingredient cache, reloaded when the cache is re-synced."""

    def __init__(self, cache_path: Path, include_vat: bool) -> None:
        self.cache_path = Path(cache_path)
        self.include_vat = include_vat
        self._lock = threading.Lock()
        self._timestamp: datetime | None = None
        self._products: list[Product] = []
        self._by_handle: dict[str, Product] = {}
        self._normalized: list[str] = []

    # -- loading -----------------------------------------------------------

    def _ensure_loaded(self) -> None:
        ts = store.cache_timestamp(self.cache_path)
        if ts == self._timestamp:
            return
        with self._lock:
            if ts == self._timestamp:
                return
            products = store.load_ingredients(self.cache_path)
            self._products = products
            self._by_handle = {_handle_key(p.handle): p for p in products}
            self._normalized = [utils.default_process(normalize(p.title)) for p in products]
            self._timestamp = ts

    @property
    def cache_timestamp(self) -> datetime:
        self._ensure_loaded()
        assert self._timestamp is not None
        return self._timestamp

    # -- queries -----------------------------------------------------------

    def get(self, handle: str) -> Product | None:
        """Look up a product by handle (leading/trailing slashes are forgiven)."""
        self._ensure_loaded()
        if not handle or not handle.strip("/ "):
            return None
        return self._by_handle.get(_handle_key(handle))

    def search(self, query: str, ingredient_type: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Top candidates for ``query``, best first; ``[]`` when nothing clears the floor.

        Out-of-stock products are included, flagged ``in_stock=False``, so the
        agent can see that the right product exists but must be substituted.
        """
        self._ensure_loaded()
        limit = max(1, min(int(limit), MAX_LIMIT))
        q = utils.default_process(normalize(query or ""))
        if not q:
            return []
        choices = {
            i: text
            for i, (p, text) in enumerate(zip(self._products, self._normalized, strict=True))
            if ingredient_type is None or p.ingredient_type == ingredient_type
        }
        if not choices:
            return []
        hits = process.extract(
            q, choices, scorer=fuzz.WRatio, processor=None, limit=None, score_cutoff=SCORE_FLOOR
        )
        # Stable order: score desc, then in-stock first, then title.
        ranked = sorted(
            hits,
            key=lambda h: (-h[1], not self._products[h[2]].in_stock(), self._products[h[2]].title),
        )
        return [self._candidate(self._products[idx], score) for _, score, idx in ranked[:limit]]

    def _candidate(self, p: Product, score: float) -> dict[str, Any]:
        return {
            "handle": p.handle,
            "title": p.title,
            "score": round(float(score), 1),
            "in_stock": p.in_stock(),
            "stock": p.stock_without_reservation,
            "price": p.price(self.include_vat),
            "ingredient_type": p.ingredient_type,
        }
