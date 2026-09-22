"""In-memory fuzzy search over the cached ingredient catalog (plan.md, step 3).

The index loads the SQLite cache once per process and reloads it only when
the cache's ``fetched_at`` changes (i.e. after a sync). Matching is a
category pre-filter by ingredient type followed by ``rapidfuzz`` WRatio over
lowercased titles with packaging noise ("11,5 g.", "1 kg", "100 g") removed.
Origin words in the query ("Belgian", "UK") are split off before matching and
only break ties between equally good titles (issue #1, ``catalog/origin.py``).
So does the recipe's beer style ("Helles" -> German malt first): candidates are
ranked and one is marked ``suggested``, but none is ever dropped.
The tool returns candidates; the agent, not the tool, picks the final match.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process, utils

from brewchat.catalog import store
from brewchat.catalog.origin import product_origin, split_query_origins, style_origins
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

    def __init__(
        self,
        cache_path: Path,
        include_vat: bool,
        origins: Mapping[str, str] | None = None,
        style_table: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.include_vat = include_vat
        self.origins: dict[str, str] = dict(origins or {})  # producer name -> ISO country code
        self.style_table: dict[str, tuple[str, ...]] = dict(style_table or {})  # style -> usual origins
        self._lock = threading.Lock()
        self._timestamp: datetime | None = None
        self._products: list[Product] = []
        self._by_handle: dict[str, Product] = {}
        self._normalized: list[str] = []
        self._origin: list[str | None] = []

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
            self._origin = [product_origin(p.title, self.origins) for p in products]
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

    def search(
        self, query: str, ingredient_type: str | None = None, limit: int = 5, style: str | None = None
    ) -> list[dict[str, Any]]:
        """Top candidates for ``query``, best first; ``[]`` when nothing clears the floor.

        Out-of-stock products are included, flagged ``in_stock=False``, so the
        agent can see that the right product exists but must be substituted.

        Origin words in the query ("Belgian Pilsner malt") are matched against
        the text without them, so they don't drag every "malt" title to the
        same score. They never lift a worse product over a better one: among
        candidates with the same score, those whose origin was asked for come
        first, then those from the origin the recipe's ``style`` usually calls
        for (a helles: German), then in-stock ones.

        The best in-stock candidate among the top-scoring ones is marked
        ``suggested`` with a ``why`` naming the tie-break that put it there.
        The others stay in the list: choosing is the user's call.
        """
        self._ensure_loaded()
        limit = max(1, min(int(limit), MAX_LIMIT))
        text, wanted = split_query_origins(query or "")
        q = utils.default_process(normalize(text))
        if not q:
            return []
        choices = {
            i: t
            for i, (p, t) in enumerate(zip(self._products, self._normalized, strict=True))
            if ingredient_type is None or p.ingredient_type == ingredient_type
        }
        if not choices:
            return []
        scores = self._scores(q, choices)
        if wanted:
            # A title can contain the origin word itself ("Belgian Ale" yeast):
            # also scoring the full query means stripping never lowers a score.
            full = utils.default_process(normalize(query))
            for idx, score in self._scores(full, choices).items():
                scores[idx] = max(score, scores.get(idx, 0.0))
        styled = style_origins(style, self.style_table)
        # Stable order: score desc, asked-for origin, the style's origin, in-stock, title.
        ranked = sorted(
            ((round(score, 1), idx) for idx, score in scores.items()),
            key=lambda h: (
                -h[0],
                bool(wanted) and self._origin[h[1]] not in wanted,
                bool(styled) and self._origin[h[1]] not in styled,
                not self._products[h[1]].in_stock(),
                self._products[h[1]].title,
            ),
        )
        out = [self._candidate(idx, score) for score, idx in ranked[:limit]]
        self._mark_suggestion(out, wanted, styled, style)
        return out

    @staticmethod
    def _mark_suggestion(
        out: list[dict[str, Any]], wanted: frozenset[str], styled: frozenset[str], style: str | None
    ) -> None:
        """Set ``suggested``/``why`` on each candidate; only a top-scoring, in-stock one is suggested."""
        for c in out:
            c["suggested"] = False
            c["why"] = None
        if not out:
            return
        top = [c for c in out if c["score"] == out[0]["score"]]
        pick = next((c for c in top if c["in_stock"]), None)
        if pick is None:
            return
        pick["suggested"] = True
        if len(top) == 1:
            return
        if wanted and pick["origin"] in wanted:
            pick["why"] = f"origin {pick['origin']} was asked for in the query"
        elif styled and pick["origin"] in styled:
            pick["why"] = f"{pick['origin']} is the usual origin for {style.strip()}"
        else:
            pick["why"] = "first of several equally good matches; no origin or style preference decides it"

    @staticmethod
    def _scores(q: str, choices: dict[int, str]) -> dict[int, float]:
        hits = process.extract(
            q, choices, scorer=fuzz.WRatio, processor=None, limit=None, score_cutoff=SCORE_FLOOR
        )
        return {idx: float(score) for _, score, idx in hits}

    def _candidate(self, idx: int, score: float) -> dict[str, Any]:
        p = self._products[idx]
        return {
            "handle": p.handle,
            "title": p.title,
            "score": round(float(score), 1),
            "in_stock": p.in_stock(),
            "price": p.price(self.include_vat),
            "pack_size": pack.label if (pack := p.pack_size()) else None,
            "ingredient_type": p.ingredient_type,
            "origin": self._origin[idx],
        }
