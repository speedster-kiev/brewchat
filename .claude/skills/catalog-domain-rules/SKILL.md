---
name: catalog-domain-rules
description: Use whenever touching brewchat/catalog/pack.py, brewchat/catalog/origin.py, brewchat/catalog/search.py, brewchat/units.py, or the stock/quantity logic in build_order_list — these encode non-obvious domain rules (pack-size parsing, country-of-origin inference, style/origin tie-breaks, in-stock definition) that are easy to get subtly wrong.
---

# BrewChat catalog domain rules

These invariants live in code and config, not in the model prompt. They're
easy to break with a locally-reasonable-looking change because the reasoning
depends on the shape of the real (anonymized) catalog data, not on anything
visible in a single function.

## Pack size (`catalog/pack.py`, issue #2)

- Pack size is embedded in the free-text product title: `"pr. 100 g."`,
  `"pr. 25 kg."`, `"100 g"`, `"11,5 g."`. The price is per **one such unit**.
- A title with no parseable size returns `None` from `Product.pack_size()`
  and is priced per pack.
- When a title has several sizes, an explicit `"pr. <size>"` wins; otherwise
  the **last** size in the title is used (`"Crystal Malt (approx. 60L), 1 kg"`
  → 1 kg, not 60 L).
- Litres need a space before the unit (`"30 L"`) so a Lovibond colour like
  `"60L"` is never misread as a pack size.
- `build_order_list` recomputes `quantity` from the recipe amount whenever
  units are comparable (mass/volume, via `brewchat/units.py`, shared with the
  eval scorer) and only trusts the model's own quantity for per-pack items.
- `search.normalize()` strips pack sizes out of titles, but **only for
  scoring** — never for pricing.
- After changing parsing logic, audit against the real cached catalog:
  `uv run scripts/check_pack_sizes.py`.

## Country of origin (`catalog/origin.py`, issue #1)

- Origin is **derived, never stored**. `product_origin()` maps a title to an
  ISO 3166-1 alpha-2 code, first via the producer map in
  `config/supplier.local.toml` (`[origins]`, shop-specific), then via a bare
  country token in the title (`UK`, `CZ`, ...).
- `ES` is deliberately **not** read as Spain — it appears in organic
  (`"Økologisk"`) product lines and doesn't reliably mean a country. Don't
  "fix" this without checking real titles first.
- `UK` is normalized to `GB` (`CODE_ALIASES`); ISO alpha-2 is the canonical
  form everywhere internally.
- `origin: null` means unknown — never treat it as "no preference" in a way
  that changes ranking; it just means the tie-break tier for that candidate
  is empty.
- `split_query_origins()` pulls origin words ("Belgian", "UK") out of a
  search query before fuzzy matching, because they're noise for text scoring
  but a signal for ranking.
- After changing producer/origin config or parsing, check coverage:
  `uv run scripts/check_origins.py` (lists producers with no mapping).

## Style and search ranking (`catalog/search.py`)

- Beer style (`Session.style`, set by `submit_parsed_recipe`) **ranks, it
  never filters**. It's a tie-break tier via `[style_origins]` config
  (style → usual origin codes), applied only among candidates that already
  scored equally on text match.
- Stable tie-break order: text score (desc) → origin asked for in the query →
  the style's usual origin → in-stock → title. An origin/style match can
  never promote a worse text match above a better one.
- Candidates carry `suggested` (best in-stock among the top scorers) and
  `why` (which tier decided it). Out-of-stock candidates are flagged, never
  hidden from the model.
- **In-stock** is a fixed definition, checked in one place:
  `StockWithoutReservation > 0 and Buyable and not Soldout`. `Online` is
  deliberately ignored — it's false on every real product, so using it would
  hide everything.

## If you're not sure a change is safe

Run the targeted test files (`tests/test_pack.py`, `tests/test_origin.py`,
`tests/test_search.py`) plus the relevant audit script above. If the change affects
what candidates the agent sees or how it talks about substitutions/origin,
also see the `system-prompt-edit` skill — style/origin reply behavior is
covered by eval fixture D6, not by unit tests.
