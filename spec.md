# BrewChat: recipe-to-basket MVP spec

Produced from [intent.md](./intent.md), read that first for the problem, the proposed outcome, constraints, and open questions at the business level. This document covers requirements, architecture, and data schemas for the P0 build.

## User stories

- As a homebrewer, I want to paste a recipe as plain text and get back a standardized ingredient list, so I don't have to manually reformat it.
- As a homebrewer, I want each ingredient matched to a real product and price from the supplier, so I know what I can actually buy.
- As a homebrewer, I want a substitution suggested when something's unavailable, with a short reason (similar alpha acid %, similar grain color/type, similar attenuation), so I trust the swap.
- As a homebrewer, I want a total estimated cost and a clean list I can copy or hand to the supplier, so checkout is one less step.
- As a homebrewer, if the recipe can't be parsed, I want a clear message telling me what's wrong, not a silent failure.
- As a homebrewer, if no reasonable substitution exists for something, I want that flagged honestly rather than a forced bad match.

## Requirements

### Must-have (P0)

- Plain-text recipe input, parsed by the agent into the standardized ingredient schema (see Data schemas below) as an explicit structured checkpoint before any catalog lookups happen.
  - Acceptance: given a recipe in a common format (grain/hop/yeast lines with amounts), the agent emits a complete structured ingredient list with at least 90% field accuracy on a test set of 10 recipes.
- Ingredient matching against a cached copy of the supplier's catalog via a `search_catalog` tool (fuzzy/text search over product `Title` and category).
  - Acceptance: given a parsed ingredient, the tool returns the best-matching product (with stock, price, link) or explicitly returns "no match."
- Substitution suggestions for unmatched or out-of-stock ingredients, reasoned directly by the agent using its own trained brewing knowledge, no external rule table. The agent reads the spec details embedded in the catalog's free-text `Title` field (e.g. "ebc 3 - 5 EBC") the same way a brewer would, proposes a substitute, then calls `search_catalog` again to confirm the substitute is actually in stock.
  - Acceptance: given an unmatched ingredient, the agent either returns a substitute (confirmed available via a follow-up catalog lookup) with a one-line reason, or explicitly states none was found.
- Final output: a formatted list with matched/substituted items, quantities, unit prices, and a total, assembled via a `build_order_list` tool that turns each product's `Handle` into a full product URL.
  - Acceptance: the list is human-readable and copyable, every line item shows source (matched vs. substituted), price, and a working link to the product page.
- Periodic sync of supplier inventory and catalog data via his API into a local cache (the API is read-only inventory + catalog, not live per-request; a single call to `/products/all` returns the full product list, no per-product fetch needed).
  - Acceptance: cache refreshes on a schedule (e.g. daily) and the demo works against cached data without live API calls per user request.
  - Acceptance: the cache is filtered to the `CategoryId` values that are actually brewing ingredients (see Data schemas below for the working category list), so the agent isn't offered equipment, kegs, or cleaning supplies as matches or substitutes.
- Topic scoping: the assistant only engages with beer recipes, brewing ingredients, and substitutions, and redirects anything else rather than answering it.
  - Acceptance: a gate step (rule-based keyword check plus a fast/cheap LLM classification call) runs before the main pipeline and blocks off-topic messages before they reach the parser or matcher.
  - Acceptance: the system prompt explicitly defines the assistant's role and includes example redirect language for off-topic requests.
  - Acceptance: the assistant's tool access is limited to the recipe parser and catalog matcher only, no general web access or open-ended code execution.
  - Acceptance: a test set of adversarial prompts (direct off-topic questions, topic-drift attempts, prompt injection embedded in a pasted recipe) is run before the demo and all are correctly redirected.

### Nice-to-have (P1)

- Recipe input via URL (e.g. paste a Brewer's Friend link) in addition to plain text.
- Confidence score or short explanation on why an ingredient was auto-matched (not just for substitutions).
- Chat-style back-and-forth to let the user override a suggested substitution before finalizing the list.

### Future considerations (P2)

- BeerXML file upload as a third input format.
- Cart/order creation once (if) the supplier opens that part of his API.
- Multi-supplier support and price comparison.
- Embeddable widget script for the supplier to drop directly into his site.

## Architecture sketch

BrewChat is a single Claude agent in a tool-use loop, not a multi-stage pipeline of separate services. The chat frontend sends the user's message to the backend, which runs Claude (via the Anthropic API, using the Tool Runner to manage the agentic loop) with three tools available:

```
[User: pastes recipe text in chat]
        |
        v
[Chat frontend]  --  sends message to backend
        |
        v
[Backend: Claude agent loop, via Tool Runner]
        |
        |-- tool: submit_parsed_recipe(ingredients[])
        |     structured checkpoint: agent's own parse of the recipe
        |     into the standardized ingredient schema, before any
        |     catalog lookups happen
        |
        |-- tool: search_catalog(query, category?)
        |     fuzzy/text search over the cached catalog (Title,
        |     category, stock, price, link); called once per
        |     ingredient, and again for any substitute the agent
        |     proposes, to confirm it's actually in stock
        |
        |     (no separate substitution tool or subagent -- when a
        |      lookup returns no match, the same agent reasons about
        |      a substitute using its own trained knowledge, reading
        |      the spec details embedded in the catalog's free-text
        |      Title field, then re-queries search_catalog to confirm)
        |
        |-- tool: build_order_list(items[])
        |     final formatting step: turns each product's Handle
        |     into a full URL, computes the total, returns the
        |     shopping list
        v
[Chat UI]  -->  formatted shopping list (copy/export)

Background job: [Supplier inventory API] --(scheduled sync)--> [Local catalog cache]
   note: /products/all returns the full product list (1771 products,
   confirmed from a real export) in one call, so the sync job is a
   single fetch, not one call per product. The per-product detail
   endpoint is still useful for spot checks but isn't needed for the
   bulk sync.
```

Notes:

- The catalog cache is the load-bearing piece. Since the supplier's API gives inventory and pricing but not order creation, the "basket" is a generated list, not a real cart, for this version.
- Substitution is intentionally not a rule table or a separate subagent. The agent reasons on its own trained brewing knowledge in the same loop, then verifies the substitute against the real catalog before offering it. This keeps P0 simpler to build and debug than a rule-engine or multi-agent setup.
- Recommended stack: Python backend using the Anthropic API's Tool Runner (`client.beta.messages.tool_runner`) to handle the agent loop over the three tools above, a cached catalog table (e.g. Postgres) populated by a scheduled sync job (cron or GitHub Actions), and a minimal standalone web chat frontend to avoid dependency on the supplier's site for this phase.

## Data schemas

**Standardized recipe ingredient** (output of `submit_parsed_recipe`), deliberately loose on spec since the agent reasons over free text on both sides of the match rather than needing rigid numeric fields:

```json
{
  "type": "fermentable | hop | yeast | other",
  "name": "string",
  "amount": "number",
  "unit": "string",
  "timing": "string | null",
  "spec": "string | null"
}
```

`spec` carries whatever the recipe states in its own words (e.g. "8% AA", "120 EBC", "75% attenuation"). It's matched against the catalog's free-text `Title` field, not a structured field on the supplier's side.

**Supplier catalog product** (real shape, confirmed from both `/json/products/all` and `/json/products/id/{id}`, same product structure in both): the relevant fields are `Id`, `ItemNumber`, `Title` (name plus embedded spec text, e.g. "ebc 3 - 5 EBC"), `CategoryId`, `SecondaryCategoryIds`, `CategoryTitle`, `Stock` / `StockWithoutReservation`, `Soldout`, `Buyable`, `Online`, `Prices` (array with `PriceMinWithVat` / `PriceMinWithoutVat`), and `Handle` (URL slug, product link is the site base URL plus `Handle`). The cache should project down to these fields rather than storing the full raw response, most of the remaining fields (dates, VAT group IDs, packet IDs, etc.) aren't needed for matching or display.

**Brewing-ingredient category filter**, from a real export of the supplier's full catalog (1771 products across ~40 categories). Working list of `CategoryId` values that are actual brewing ingredients, everything else in the catalog is equipment, kegs, bottles, cleaning supplies, or hardware-specific accessories (Brewtools, Grainfather, SS Brewtech, etc.) and should be excluded from the cache the agent searches:

| CategoryId | Title (Danish) | Ingredient type |
|---|---|---|
| 6 | Malt | fermentable |
| 34 | Karamel malt | fermentable |
| 37 | Special malt | fermentable |
| 39 | Økologisk malt | fermentable |
| 263 | Hele 25 kg. maltsække | fermentable |
| 64 | Div. flager, havre - majs - hvede - ris m.fl. | fermentable (adjuncts) |
| 7 | Humle | hop |
| 53 | Europæiske humler | hop |
| 8 | Gær | yeast |
| 282 | Lallemand Tørgær | yeast |
| 19 | Tilsætning - Krydderier, sukker, sirup m.m. | other |

This is a first pass from the category names and counts, not confirmed with the supplier yet, see [intent.md](./intent.md) open questions.

## Success metrics

**Leading (days to weeks):**
- Recipe parse success rate across a test set of real recipes: target 90%+.
- Ingredient match rate against the supplier's live catalog: target 80%+.
- Substitution acceptance: when you (or test users) review suggested substitutions, target 90%+ judged reasonable.

**Lagging (the metrics that actually matter here):**
- Supplier agrees to a real pilot or site integration: this is the primary goal of the demo.
- If a pilot happens: number of real orders placed by users who came through the tool, as a starting point for any monetization conversation (see [intent.md](./intent.md) for the monetization open question).

## Open questions

Business and supplier-facing open questions live in [intent.md](./intent.md). The one purely engineering question specific to this spec:

- Does the supplier's API expose real-time stock, or only periodic snapshots? (engineering, need to check API docs with him, affects how often the sync job should run)

## Timeline considerations

No hard deadline set yet. Suggested phasing: 2-3 weeks to build the P0 scope end to end against a real (or representative) slice of the supplier's catalog, then a live walkthrough with him as the pitch moment, rather than a written proposal first.

## Before handover to Claude Code

Gaps found in a review pass, worth closing before development starts.

**Blocking:**
- The "Topic scoping" requirement (P0) still describes an old pre-agent design, a separate "gate step before the main pipeline" and a "recipe parser and catalog matcher" as distinct tools. This contradicts the Architecture sketch (one agent, three tools: `submit_parsed_recipe`, `search_catalog`, `build_order_list`). Needs to be rewritten to match, and a decision made on whether a cheap pre-classification call still runs before the agent, or whether system-prompt scoping alone is the gate.
- No test recipe fixtures exist. Several P0 acceptance criteria reference "a test set of 10 recipes," but only the two HopCellar catalog exports are in the folder. Need actual sample recipes (and a set of adversarial off-topic/injection prompts for the topic-scoping tests) before Claude Code can validate against anything real.
- Currency/VAT assumption (DKK, `PriceMinWithVat` vs. without) is unconfirmed, blocks finishing `build_order_list`. See intent.md.
- How the supplier wants to receive generated orders (formatted list, email, other) is unconfirmed, blocks finalizing the output format. See intent.md.

**Should resolve before or during build:**
- Matching implementation for `search_catalog` is underspecified, "fuzzy/text search" could mean a Postgres trigram search, a library like rapidfuzz, or a keyword pre-filter with the agent picking the best candidate. These behave very differently on real product names. Needs a decision, not left to whichever Claude Code defaults to.
- No frontend/backend API contract written down (endpoint shape, streaming vs. single response, how conversation history is carried within a session).
- No access control for the demo. It'll be running against the supplier's real stock and prices before he's agreed to anything, worth at least a shared password gate.
- No guardrail requiring substitution suggestions to be flagged as advisory or to show the agent's reasoning inline, worth adding given there's no rule table backing them.
- Stack/hosting decisions (Render/Railway, Neon, GitHub Actions cron, Vercel, Tool Runner) were settled in conversation but only partially made it into the Architecture sketch. Should be stated explicitly so Claude Code isn't left to choose.
- The brewing-ingredient category filter (Data schemas) is a first pass from category names/counts, not confirmed with the supplier, worth a quick sanity check on the edge cases (kits, cask/specialty beer).
