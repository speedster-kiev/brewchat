"""Recipe and catalog units, collapsed into three families.

Shared by the order list (pack-size arithmetic in ``build_order_list``) and the
parse eval scorer, so both agree on what "1 lb" or "2 tsk" means.
"""

from __future__ import annotations

import re

_MASS_G = {
    "mg": 0.001,
    "g": 1.0,
    "gr": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "gramm": 1.0,
    "kg": 1000.0,
    "kgs": 1000.0,
    "kilo": 1000.0,
    "kilos": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "oz": 28.349523125,
    "ounce": 28.349523125,
    "ounces": 28.349523125,
    "lb": 453.59237,
    "lbs": 453.59237,
    "pound": 453.59237,
    "pounds": 453.59237,
}
_VOLUME_ML = {
    "ml": 1.0,
    "milliliter": 1.0,
    "millilitre": 1.0,
    "milliliters": 1.0,
    "millilitres": 1.0,
    "cl": 10.0,
    "dl": 100.0,
    "l": 1000.0,
    "liter": 1000.0,
    "litre": 1000.0,
    "liters": 1000.0,
    "litres": 1000.0,
    "tsp": 4.92892,
    "teaspoon": 4.92892,
    "teaspoons": 4.92892,
    "tsk": 4.92892,
    "tbsp": 14.7868,
    "tablespoon": 14.7868,
    "tablespoons": 14.7868,
    "spsk": 14.7868,
}
# Countable purchase units. All collapse to one family: "1 pack" and "1 vial" of
# a liquid yeast, or "1 each" and "1 tablet" of Whirlfloc, are the same amount.
_COUNT = {
    "",
    "x",
    "each",
    "ea",
    "unit",
    "units",
    "piece",
    "pieces",
    "pc",
    "pcs",
    "whole",
    "pack",
    "packs",
    "packet",
    "packets",
    "pk",
    "pkg",
    "pkgs",
    "pkt",
    "pkts",
    "package",
    "packages",
    "sachet",
    "sachets",
    "vial",
    "vials",
    "pouch",
    "pouches",
    "smack pack",
    "smack packs",
    "smackpack",
    "tube",
    "tubes",
    "tablet",
    "tablets",
    "tab",
    "tabs",
    "stick",
    "sticks",
    "stk",
    "stykke",
    "stykker",
    "pose",
    "poser",
    "can",
    "cans",
    "tin",
    "tins",
}


def normalize_unit(amount: float, unit: str | None) -> tuple[str, float]:
    """Return ``(family, amount_in_canonical_unit)``.

    Families: ``mass`` (grams), ``volume`` (ml), ``count`` (packs/each/...). An
    unrecognised unit becomes its own family ``raw:<unit>``, so it only matches
    the identical string.
    """
    u = (unit or "").strip().lower().rstrip(".")
    u = re.sub(r"\s+", " ", u)
    if u in _MASS_G:
        return "mass", float(amount) * _MASS_G[u]
    if u in _VOLUME_ML:
        return "volume", float(amount) * _VOLUME_ML[u]
    if u in _COUNT:
        return "count", float(amount)
    return f"raw:{u}", float(amount)
