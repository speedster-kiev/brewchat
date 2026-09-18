"""Pydantic models for the tool inputs and the order list (spec.md, Data schemas)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

IngredientType = Literal["fermentable", "hop", "yeast", "other"]
Source = Literal["matched", "substituted", "user_override", "unavailable"]
Confidence = Literal["high", "medium", "low"]


class Ingredient(BaseModel):
    """One recipe ingredient, as parsed by the agent. Deliberately loose on spec."""

    type: IngredientType
    name: str = Field(min_length=1)
    amount: float
    unit: str
    timing: str | None = None
    spec: str | None = Field(
        default=None, description="What the recipe states in its own words, e.g. '8% AA', '120 EBC'."
    )


class Substitution(BaseModel):
    original_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    confidence: Confidence


class OrderListItem(BaseModel):
    ingredient: Ingredient
    product_handle: str | None = None
    quantity: float = Field(
        gt=0,
        description=(
            "Number of catalog units to buy. Recalculated from ingredient.amount when the product has a "
            "weight or volume pack_size, so it only decides for per-pack items such as yeast."
        ),
    )
    source: Source
    substitution: Substitution | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> OrderListItem:
        needs_sub = self.source in ("substituted", "user_override")
        if needs_sub and self.substitution is None:
            raise ValueError(f"source={self.source!r} requires a substitution")
        if not needs_sub and self.substitution is not None:
            raise ValueError(f"source={self.source!r} must not carry a substitution")
        if self.source != "unavailable" and not self.product_handle:
            raise ValueError(f"source={self.source!r} requires a product_handle")
        return self


class OrderLine(BaseModel):
    """One rendered line of the final list (output side, links resolved)."""

    ingredient_name: str
    ingredient_amount: float
    ingredient_unit: str
    source: Source
    product_title: str | None
    product_handle: str | None
    url: str | None
    quantity: float
    pack_size: str | None = None  # e.g. "100 g": what one unit of quantity stands for
    quantity_requested: float | None = None  # the agent's quantity, set only when code corrected it
    unit_price: float | None
    line_total: float | None
    in_stock: bool | None
    substitution: Substitution | None = None


class OrderList(BaseModel):
    items: list[OrderLine]
    total: float
    currency: str
    vat_note: str
    cache_timestamp: str
    handoff_note: str
    text: str = Field(description="Plain-text rendering, copyable in one action.")
