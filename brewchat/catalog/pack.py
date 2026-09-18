"""Pack size embedded in catalog titles (issue #2).

The shop puts the unit a price applies to in the free-text title, in a few
shapes: "..., pr. 100 g." or "pr. 25 kg sæk" (price per 100 g / per sack, the
quantity counts those units), "..., 100 g." or "300 g" (a fixed hop or sugar
pack), "11,5 g." (a yeast sachet). All of them mean the same thing for an
order: the price is for one unit of that size, so the quantity to buy is the
recipe amount divided by the pack size, rounded up. A title without a size is
priced per pack and returns ``None``.

When a title holds several sizes, an explicit "pr. <size>" wins, otherwise the
last size is used ("Crystal Malt (approx. 60L), 1 kg" is 1 kg). Litres need a
space before the unit ("30 L"), so a Lovibond colour like "60L" is not a pack.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from brewchat.units import normalize_unit

_SIZE = re.compile(
    r"(?<![\w.,])(?P<pr>pr\.?\s*)?(?P<amount>\d+(?:[.,]\d+)?)"
    r"(?:\s*(?P<unit>kg|gr|g|ml|cl)|\s+(?P<litre>l|ltr|liter|litre))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PackSize:
    amount: float  # as written in the title, e.g. 100 or 25
    unit: str  # lowercased, litre spellings as "l": "g", "kg", "ml", "l", ...
    family: str  # "mass" or "volume" (see brewchat.units)
    base_amount: float  # in grams or millilitres

    @property
    def label(self) -> str:
        return f"{self.amount:g} {self.unit}"


def parse_pack_size(title: str) -> PackSize | None:
    """The size one catalog unit of this product stands for, or ``None`` (priced per pack)."""
    matches = list(_SIZE.finditer(title or ""))
    if not matches:
        return None
    m = next((m for m in matches if m["pr"]), matches[-1])
    amount = float(m["amount"].replace(",", "."))
    if amount <= 0:
        return None
    unit = "l" if m["litre"] else m["unit"].lower()
    family, base = normalize_unit(amount, unit)
    return PackSize(amount=amount, unit=unit, family=family, base_amount=base)


def units_needed(amount: float, unit: str, pack: PackSize | None) -> int | None:
    """Catalog units to buy for ``amount unit`` of this pack, rounded up.

    ``None`` when the recipe unit can't be compared with the pack (no pack size,
    or e.g. "1 pack" of yeast against an 11,5 g sachet): the caller then keeps
    the quantity the agent chose.
    """
    if pack is None:
        return None
    family, need = normalize_unit(amount, unit)
    if family != pack.family or need <= 0:
        return None
    # Round the ratio first so float noise (0.3 kg = 300.00000000000006 g) can't add a pack.
    return max(1, math.ceil(round(need / pack.base_amount, 6)))
