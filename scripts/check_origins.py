"""Country of origin across the cached catalog (issue #1).

`uv run scripts/check_origins.py` prints, per ingredient type, how many
products got an origin and from where (producer map or a country token in the
title), then the producers seen in titles that have no `[origins]` entry, and
the hop titles that name no country. Run it after each sync: a new maltster
shows up here until it is added to the local supplier config. Reads the local
cache only; nothing leaves the machine.
"""

import sys
from collections import Counter

from brewchat.catalog.origin import producer_candidate, product_origin
from brewchat.catalog.store import CacheMissingError, load_ingredients
from brewchat.config import ConfigError, load_env_file, load_settings


def main() -> int:
    load_env_file()
    try:
        settings = load_settings()
        products = load_ingredients(settings.cache_path)
    except (ConfigError, CacheMissingError) as exc:
        print(exc, file=sys.stderr)
        return 1
    mapped = {name.lower() for name in settings.origins}
    by_type: Counter[tuple[str, str]] = Counter()
    unmapped: Counter[str] = Counter()
    hops_without: list[str] = []
    for p in products:
        origin = product_origin(p.title, settings.origins)
        by_type[(p.ingredient_type, origin or "unknown")] += 1
        # Only malts and adjuncts read "<product> - <producer>"; yeast reads "<brand> - <strain>".
        producer = producer_candidate(p.title) if p.ingredient_type in ("fermentable", "other") else None
        if producer and not any(m in p.title.lower() for m in mapped):
            unmapped[producer] += 1
        if p.ingredient_type == "hop" and origin is None:
            hops_without.append(p.title)
    for (itype, origin), n in sorted(by_type.items()):
        print(f"{itype:12} {origin:8} {n:4}")
    print(f"\n{len(unmapped)} producers in titles with no [origins] entry (n = products):")
    for name, n in unmapped.most_common():
        print(f"  {n:4}  {name}")
    print(f"\n{len(hops_without)} hop titles with no country token:")
    for title in hops_without:
        print(f"  {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
