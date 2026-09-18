"""Print per-category product counts from a raw products-all export.

Local use only (the export is gitignored). Shows which categories the config
maps to an ingredient type, so the map can be re-checked after each sync:

    uv run scripts/check_categories.py path/to/export.json
"""

import json
import sys
from collections import Counter

from brewchat.config import EXAMPLE_CONFIG, LOCAL_CONFIG, load_settings


def main(path: str) -> int:
    settings = load_settings(LOCAL_CONFIG if LOCAL_CONFIG.is_file() else EXAMPLE_CONFIG)
    raw = json.load(open(path, encoding="utf-8"))
    products = raw["products"] if isinstance(raw, dict) else raw
    counts = Counter((p["CategoryId"], p.get("CategoryTitle") or "") for p in products)
    included = 0
    print(f"{'id':>5}  {'n':>4}  {'type':<12} title")
    for (cid, title), n in sorted(counts.items()):
        itype = settings.categories.get(cid, "")
        print(f"{cid:>5}  {n:>4}  {itype:<12} {title}")
    for p in products:
        secondary = [int(s) for s in p.get("SecondaryCategoryIds") or [] if str(s).isdigit()]
        if settings.ingredient_type_for(p["CategoryId"], secondary):
            included += 1
    print(f"\n{len(products)} products, {included} included by the category map "
          "(primary match, or secondary match unless excluded)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
