"""Projected catalog product: the only fields the cache keeps from the raw export."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from brewchat.catalog.pack import PackSize, parse_pack_size


@dataclass(frozen=True)
class Product:
    id: int
    item_number: str | None
    title: str
    category_id: int
    secondary_category_ids: list[int]
    category_title: str | None
    stock: float
    stock_without_reservation: float
    soldout: bool
    buyable: bool
    online: bool  # projected but unused: False on every product in the July export
    price_with_vat: float
    price_without_vat: float
    handle: str  # full path, e.g. "/shop/8-gaer/166-.../"; link = base_url + handle
    ingredient_type: str

    def in_stock(self) -> bool:
        # `Online` is deliberately ignored (plan.md, finding 1).
        return self.stock_without_reservation > 0 and self.buyable and not self.soldout

    def price(self, include_vat: bool) -> float:
        return self.price_with_vat if include_vat else self.price_without_vat

    def pack_size(self) -> PackSize | None:
        # Derived from the title, not stored: the cache keeps the raw title.
        return parse_pack_size(self.title)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project(raw: dict[str, Any], ingredient_type: str) -> Product:
    """Project one raw platform product onto ``Product``. Raises KeyError/TypeError on shape drift."""
    price = raw["Prices"][0]  # always a single-element array in practice
    return Product(
        id=int(raw["Id"]),
        item_number=raw.get("ItemNumber"),
        title=str(raw["Title"]).strip(),
        category_id=int(raw["CategoryId"]),
        secondary_category_ids=parse_secondary(raw.get("SecondaryCategoryIds")),
        category_title=raw.get("CategoryTitle"),
        stock=float(raw.get("Stock") or 0),
        stock_without_reservation=float(raw.get("StockWithoutReservation") or 0),
        soldout=bool(raw.get("Soldout")),
        buyable=bool(raw.get("Buyable")),
        online=bool(raw.get("Online")),
        price_with_vat=float(price["PriceMinWithVat"]),
        price_without_vat=float(price["PriceMinWithoutVat"]),
        handle=str(raw["Handle"]),
        ingredient_type=ingredient_type,
    )


def parse_secondary(value: Any) -> list[int]:
    out: list[int] = []
    for v in value or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out
