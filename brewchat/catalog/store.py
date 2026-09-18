"""Read side of the catalog cache."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from brewchat.catalog.schema import Product


class CacheMissingError(RuntimeError):
    """No catalog cache yet: run scripts/sync_catalog.py first."""


def _connect(cache_path: Path) -> sqlite3.Connection:
    if not cache_path.is_file():
        raise CacheMissingError(
            "Catalog cache not found. Run `uv run scripts/sync_catalog.py` first."
        )
    return sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)


def load_ingredients(cache_path: Path) -> list[Product]:
    conn = _connect(cache_path)
    try:
        rows = conn.execute(
            "SELECT id, item_number, title, category_id, secondary_category_ids, category_title,"
            " stock, stock_without_reservation, soldout, buyable, online, price_with_vat,"
            " price_without_vat, handle, ingredient_type FROM products ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [
        Product(
            id=r[0], item_number=r[1], title=r[2], category_id=r[3],
            secondary_category_ids=json.loads(r[4]), category_title=r[5],
            stock=r[6], stock_without_reservation=r[7], soldout=bool(r[8]),
            buyable=bool(r[9]), online=bool(r[10]), price_with_vat=r[11],
            price_without_vat=r[12], handle=r[13], ingredient_type=r[14],
        )
        for r in rows
    ]


def cache_timestamp(cache_path: Path) -> datetime:
    conn = _connect(cache_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'fetched_at'").fetchone()
    finally:
        conn.close()
    if row is None:
        raise CacheMissingError("Catalog cache has no fetched_at timestamp.")
    return datetime.fromisoformat(row[0])
