"""Pack sizes parsed from the cached catalog titles (issue #2).

`uv run scripts/check_pack_sizes.py` prints how many products per ingredient
type were read as each pack size, then every non-yeast title with no size
(priced per pack). Check that list after a sync when the shop changes its
title conventions; a title with a size the parser missed will be priced per
pack. Reads the local cache only; nothing leaves the machine.
"""

import sys
from collections import Counter

from brewchat.catalog.store import CacheMissingError, load_ingredients
from brewchat.config import ConfigError, load_env_file, load_settings


def main() -> int:
    load_env_file()
    try:
        products = load_ingredients(load_settings().cache_path)
    except (ConfigError, CacheMissingError) as exc:
        print(exc, file=sys.stderr)
        return 1
    sizes: Counter[tuple[str, str]] = Counter()
    per_pack = []
    for p in products:
        pack = p.pack_size()
        sizes[(p.ingredient_type, pack.label if pack else "per pack")] += 1
        if pack is None and p.ingredient_type != "yeast":
            per_pack.append(p)
    for (itype, label), n in sorted(sizes.items()):
        print(f"{itype:12} {label:10} {n:4}")
    print(f"\n{len(per_pack)} non-yeast products priced per pack (no size in the title):")
    for p in per_pack:
        print(f"  [{p.ingredient_type}] {p.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
