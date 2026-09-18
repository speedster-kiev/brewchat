"""Daily catalog sync: one GET to the platform's products-all endpoint.

The raw response is projected and category-filtered in memory, then written
to a fresh SQLite file that atomically replaces the previous cache. On any
failure (network, HTTP error, shape change, empty result) the previous cache
is left untouched and ``SyncError`` is raised. The raw response is never
written to disk.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from brewchat.catalog.schema import Product, project
from brewchat.config import Settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0

SCHEMA = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    item_number TEXT,
    title TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    secondary_category_ids TEXT NOT NULL,
    category_title TEXT,
    stock REAL NOT NULL,
    stock_without_reservation REAL NOT NULL,
    soldout INTEGER NOT NULL,
    buyable INTEGER NOT NULL,
    online INTEGER NOT NULL,
    price_with_vat REAL NOT NULL,
    price_without_vat REAL NOT NULL,
    handle TEXT NOT NULL,
    ingredient_type TEXT NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class SyncError(RuntimeError):
    """The sync failed; the previous cache was kept."""


def fetch_raw(settings: Settings, client: httpx.Client | None = None) -> dict[str, Any]:
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        resp = client.get(
            settings.supplier.products_all_url,
            headers={"User-Agent": settings.supplier.user_agent, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    finally:
        if own:
            client.close()


def filter_and_project(raw: Any, settings: Settings) -> list[Product]:
    """Keep only ingredient categories and project each product. Raises SyncError on shape drift."""
    if not isinstance(raw, dict) or not isinstance(raw.get("products"), list):
        raise SyncError("Unexpected response shape: no 'products' list")
    products: list[Product] = []
    for item in raw["products"]:
        try:
            secondary = [int(s) for s in item.get("SecondaryCategoryIds") or [] if str(s).isdigit()]
            itype = settings.ingredient_type_for(int(item["CategoryId"]), secondary)
            if itype is None:
                continue
            products.append(project(item, itype))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise SyncError(f"Unexpected product shape: {exc!r}") from exc
    if not products:
        raise SyncError("No ingredient products after category filter")
    return products


def write_cache(products: list[Product], cache_path: Path, fetched_at: datetime) -> None:
    """Write a new cache file next to the old one, then atomically swap it in."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".catalog-", suffix=".sqlite", dir=cache_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        conn = sqlite3.connect(tmp)
        try:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        p.id, p.item_number, p.title, p.category_id,
                        json.dumps(p.secondary_category_ids), p.category_title,
                        p.stock, p.stock_without_reservation, int(p.soldout),
                        int(p.buyable), int(p.online), p.price_with_vat,
                        p.price_without_vat, p.handle, p.ingredient_type,
                    )
                    for p in products
                ],
            )
            conn.execute("INSERT INTO meta VALUES ('fetched_at', ?)", (fetched_at.isoformat(),))
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, cache_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sync(settings: Settings, client: httpx.Client | None = None, now: datetime | None = None) -> int:
    """Fetch, filter, and replace the cache. Returns the number of products cached."""
    try:
        raw = fetch_raw(settings, client)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        # Deliberately no URL in the message: logs never carry the base URL.
        raise SyncError(f"Catalog fetch failed: {type(exc).__name__}") from exc
    products = filter_and_project(raw, settings)
    write_cache(products, settings.cache_path, now or datetime.now(UTC))
    log.info("Catalog sync wrote %d products", len(products))
    return len(products)
