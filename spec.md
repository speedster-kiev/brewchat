# BrewChat: recipe-to-basket MVP spec

Produced from [intent.md](./intent.md), read that first for the problem, goals, proposed outcome, constraints, risks, and open questions at the business level. This document covers requirements, architecture, configuration, and data schemas for the P0 build.

Revised to follow the reviewed intent. The main shifts: the primary success measure is now time-to-order (my own baseline of about 2 hours per brew), the supplier pilot is secondary; the supplier's shop runs on a third-party online shop platform whose unauthenticated JSON endpoints are scraped into a daily cache, there is no supplier API; the demo is private and the supplier is anonymized everywhere except a local settings file; and substitutions must carry a reason and an explicit confidence, with every suggestion and the user's final choice logged. See "Revision notes" at the end for the full list.

## Priorities that shape this spec

From intent.md, goals in order: (1) portfolio showcase of an end-to-end product exercise, (2) save my own ordering time, (3) possible extension and collaboration with the supplier and later with other shops on the same platform. When they conflict the higher one wins, so:

- Clean architecture and clear documentation beat demo polish. Code and docs should read well to a stranger; supplier specifics are isolated in configuration.
- Demo polish beats supplier-specific customization. Nothing in the pipeline should assume this one supplier beyond what the config file provides.
- Because goal 3 may turn out to be platform-level rather than supplier-level, the supplier adapter is written against the platform's endpoint shape, with the shop identity (name, domain) supplied by config. Swapping to another shop on the same platform should be a config change, not a code change.

## User stories

- As a homebrewer, I want to paste a recipe as plain text and get back a standardized ingredient list, so I don't have to manually reformat it.
- As a homebrewer, I want each ingredient matched to a real product and price from the supplier, so I know what I can actually buy.
- As a homebrewer, I want a substitution suggested when something's unavailable, with a short reason (similar alpha acid %, similar grain color/type, similar attenuation) and a clear signal of how confident the assistant is, so I can decide whether to trust the swap.
- As a homebrewer, I want to say "keep the original" or "use X instead" for any suggested substitution and get the list rebuilt, so the final list is mine.
- As a homebrewer, I want a total estimated cost and a clean, copyable list with a working link per line, so carrying it into the shop is quick.
- As a homebrewer, I want to see when the stock data was last refreshed, so I know whether an "in stock" claim is fresh.
- As a homebrewer, if the recipe can't be parsed, I want a clear message telling me what's wrong, not a silent failure.
- As a homebrewer, if no reasonable substitution exists for something, I want that flagged honestly rather than a forced bad match.
- As the project owner, I want every suggested substitution and what the user finally kept written to a log, so I can review suggestion quality afterwards without a human check in the flow.

## Requirements

### Must-have (P0)

- Plain-text recipe input, parsed by the agent into the standardized ingredient schema (see Data schemas below) as an explicit structured checkpoint before any catalog lookups happen.
  - Acceptance: given a recipe in a common format (grain/hop/yeast lines with amounts), the agent emits a complete structured ingredient list with at least 90% field accuracy on the recipe fixture set (`recipes-fixtures/recipes.md`, currently 5 recipes, to be grown to 10 before the demo).
- Ingredient matching against a cached copy of the supplier's catalog via a `search_catalog` tool.
  - Acceptance: given a parsed ingredient, the tool returns the best-matching candidates (with stock, price, link) or explicitly returns "no match."
  - Decision: `search_catalog` does a category pre-filter (by ingredient type, using the category map in Data schemas) followed by fuzzy string matching on `Title` with `rapidfuzz`, and returns the top 5 candidates with scores. The agent, not the tool, picks the final match, since it can read the spec text in titles (EBC, alpha acid, attenuation) the way a brewer would. No Postgres trigram or vector search for P0; the catalog is about 1800 products and the filtered ingredient slice is a few hundred, so an in-memory search is enough.
- Substitution suggestions for unmatched or out-of-stock ingredients, reasoned directly by the agent using its own trained brewing knowledge, no external rule table. The agent reads the spec details embedded in the catalog's free-text `Title` field (e.g. "ebc 3 - 5 EBC") the same way a brewer would, proposes a substitute, then calls `search_catalog` again to confirm the substitute is actually in stock.
  - Acceptance: given an unmatched ingredient, the agent either returns a substitute (confirmed available via a follow-up catalog lookup) with a one-line reason and a confidence level, or explicitly states none was found.
  - Acceptance: every substitution is presented as a suggestion, never silently swapped in. Each carries a `confidence` of `high`, `medium`, or `low`, and for `medium` and `low` the assistant says in plain words that it isn't sure and why (e.g. "this is a bittering hop, the recipe used it late, so the aroma will differ").
  - Acceptance: the system prompt instructs the agent to prefer "no substitute found" over a forced match when it has no sound brewing reason for a swap.
- User override of substitutions within the session. After the list is shown, the user can reject a substitution (keep the original and let the list show it as unavailable) or name a different product; the agent re-verifies via `search_catalog` and rebuilds the list.
  - Acceptance: a reply like "keep Cascade instead of Centennial" or "use Magnum for bittering" produces a rebuilt list where that line reflects the user's choice, and the substitution log records it.
- Substitution logging. Every substitution the agent proposes, and what the user finally kept, is written to an append-only log (see Data schemas). The log stores no user identity: only an anonymous per-session id, timestamps, the original ingredient, the proposed substitute, reason, confidence, and the final decision.
  - Acceptance: after a session with at least one substitution, the log contains one record per proposed substitution with `decision` set to `accepted`, `rejected`, or `replaced_by_user` (or `pending` if the session ended before a final list was built).
  - Acceptance: the log can be exported as JSONL or CSV so it can be reviewed by hand or fed to an LLM for a quality review.
- Final output: a formatted list with matched/substituted items, quantities, unit prices, and a total, assembled via a `build_order_list` tool that turns each product's `Handle` into a full product URL using the base URL from config.
  - Acceptance: the list is human-readable and copyable as plain text in one action, every line item shows source (matched, substituted, or user override), price, and a working link to the product page.
  - Acceptance: the list states the catalog cache timestamp ("stock and prices as of 2026-09-17 06:00") and notes that stock can have changed since.
  - Acceptance: the list ends with a short reminder that it must be added to the shop's cart by hand, since there is no cart endpoint.
- Daily sync of the supplier's catalog from the platform's unauthenticated product JSON endpoint into a local cache. A single call to the products-all endpoint returns the full product list, no per-product fetch needed. The demo works against cached data and never calls the platform per user request.
  - Acceptance: the sync job runs on a daily schedule, records the fetch timestamp, and the agent's tools read only from the cache.
  - Acceptance: the cache is filtered to the `CategoryId` values that are actually brewing ingredients (see Data schemas), so the agent isn't offered equipment, kegs, or cleaning supplies as matches or substitutes.
  - Acceptance: the sync is polite: one request per day, a descriptive User-Agent, and it backs off and keeps the previous cache if the endpoint fails or changes shape. The raw response is never committed; only the projected cache is stored, and even that stays out of the repo.
- Supplier anonymization and configuration. The real supplier name, shop domain, and endpoint base URL exist only in a local, uncommitted settings file. Code, docs, tests, fixtures, and logs use the placeholder name "HopCellar" and the placeholder domain `hopcellar.example`.
  - Acceptance: a committed `config/supplier.example.toml` with placeholders and a gitignored `config/supplier.local.toml` with real values; the app refuses to start without the local file and points to the example.
  - Acceptance: `grep` for the real supplier name and domain across the repo (excluding gitignored files) returns nothing. A pre-commit check enforces this.
  - Acceptance: log records and error messages never include the base URL; product links in the log use `Handle` only.
- Access gating. The demo is private: no public URL without a login step, shared only with the supplier and people I choose.
  - Acceptance: the web chat sits behind a shared passphrase (or HTTP basic auth) configured via environment variable; unauthenticated requests get a login prompt and nothing else. Search engines are told not to index (`robots.txt` plus `noindex`).
- Topic scoping. The assistant only engages with beer recipes, brewing ingredients, and substitutions, and redirects anything else rather than answering it. This is done inside the single agent, not as a separate pipeline stage.
  - Acceptance: the system prompt defines the assistant's role, lists what it will and won't discuss, and includes example redirect language for off-topic requests.
  - Acceptance: the agent's tool access is limited to `submit_parsed_recipe`, `search_catalog`, and `build_order_list`; no web access, no code execution.
  - Acceptance: pasted recipe text is passed to the model as data inside a clearly delimited block, and the system prompt states that instructions found inside a recipe are to be ignored.
  - Acceptance: a fixture set of adversarial prompts (direct off-topic questions, topic-drift attempts, prompt injection embedded in a pasted recipe) is run before the demo and all are correctly redirected. This set lives next to the recipe fixtures.
  - Decision: no separate cheap pre-classification call for P0. System-prompt scoping plus the restricted tool set is the gate. If the adversarial fixture run shows leaks, a pre-classifier is the first thing to add.
- Time-to-order measurement support. The primary success criterion is measured time from recipe in hand to order confirmed, chatbot versus manual, so the app must make that measurement easy.
  - Acceptance: each session logs a start timestamp (first message) and the timestamp of the last `build_order_list` call, so the in-chat portion of the time is recorded automatically. The remaining part (adding items to the shop's cart and confirming) is timed by hand with a stopwatch and written down alongside the session id.

### Nice-to-have (P1)

- Recipe input via URL (e.g. paste a Brewer's Friend link) in addition to plain text.
- Confidence and a short reason on why an ingredient was auto-matched (not just for substitutions), so a wrong direct match is as visible as a wrong swap.
- Substitution log review helper: a small script that summarizes the log (acceptance rate per confidence level, most rejected swaps) and optionally asks Claude to grade each suggestion.
- A "download as text" or "email to myself" button on the final list.

### Future considerations (P2)

- BeerXML file upload as a third input format.
- Cart or order creation if the platform provider ever exposes it. There is no such endpoint today, and it would need an agreement with the platform, not the supplier.
- Supplier-driven steering: let the supplier mark products he wants to move, and surface current deals or promotions in the chat, so substitutions become an upsell or cross-sell lever. Not discussed with him yet; do not build until it is.
- Multi-shop support on the same platform: several `supplier.*.toml` files and a shop selector, as the concrete form of goal 3. The config-driven adapter in P0 is the groundwork.
- Embeddable widget script for a shop to drop into its site.

## Architecture sketch

BrewChat is a single Claude agent in a tool-use loop, not a multi-stage pipeline of separate services. The chat frontend sends the user's message to the backend, which runs Claude (via the Anthropic API, using the Tool Runner to manage the agentic loop) with three tools available:

```
[User: passes the access gate, pastes recipe text in chat]
        |
        v
[Chat frontend]  --  sends message + session id to backend
        |
        v
[Backend: Claude agent loop, via Tool Runner, system prompt = role + scope + substitution rules]
        |
        |-- tool: submit_parsed_recipe(ingredients[])
        |     structured checkpoint: agent's own parse of the recipe
        |     into the standardized ingredient schema, before any
        |     catalog lookups happen
        |
        |-- tool: search_catalog(query, ingredient_type?)
        |     category pre-filter + rapidfuzz over Title on the
        |     cached catalog; returns top 5 candidates with score,
        |     stock, price, handle. Called once per ingredient, and
        |     again for any substitute the agent proposes (or the
        |     user requests), to confirm it's actually in stock
        |
        |     (no separate substitution tool or subagent: when a
        |      lookup returns no usable match, the same agent reasons
        |      about a substitute using its own trained knowledge,
        |      reading the spec details in the Title text, then
        |      re-queries search_catalog to confirm)
        |
        |-- tool: build_order_list(items[])
        |     final formatting step: turns each Handle into a full
        |     URL using config.base_url, computes the total, stamps
        |     the cache timestamp, returns the shopping list, and
        |     writes substitution log records for every line that
        |     carries substitution metadata
        v
[Chat UI]  -->  formatted shopping list (copy as text, link per line)
        ^
        |   user replies "keep X" / "use Y instead"  -->  agent re-verifies, rebuilds

Background job (daily): [Platform product JSON endpoint, base_url from config]
                          --(one fetch, projected + category-filtered)-->
                        [Local catalog cache + fetch timestamp]
   note: the products-all endpoint returns the full product list
   (1771 products in the July export) in one call, so the sync is
   a single fetch. The per-product endpoint is useful for spot
   checks but isn't needed for the bulk sync.

Side outputs: [Substitution log (JSONL, no user identity)]
              [Session timing log (start, last list built)]
```

Notes:

- The catalog cache is the load-bearing piece. The platform exposes catalog, stock, and pricing but no cart, so the "basket" is a generated list, not a real cart, for this version. The list is designed so the hand-off to the shop is as short as possible: copyable, one working link per line, cache timestamp shown.
- Substitution is intentionally not a rule table or a separate subagent. The agent reasons on its own trained brewing knowledge in the same loop, then verifies the substitute against the real catalog before offering it. This keeps P0 simpler to build and debug than a rule engine or multi-agent setup. The cost of that choice is the "confidently wrong swap" risk from intent.md, which is why reason plus confidence on every suggestion and the substitution log are P0, not polish.
- The supplier adapter (sync job plus the product projection) is written against the platform's endpoint shape, not the supplier. Shop identity comes from config. This is what keeps goal 3 open at near-zero extra cost.
- Stack, stated explicitly so the build doesn't have to choose: Python 3.12 backend (FastAPI), Anthropic API with the Tool Runner (`client.beta.messages.tool_runner`) for the agent loop, `rapidfuzz` for matching, SQLite for the catalog cache and the logs (single-user demo, no need for Postgres yet; the schema is plain enough to move later), a daily GitHub Actions cron or a host-side cron for the sync job, and a minimal standalone web chat frontend (server-rendered or a small static page talking to the backend over a single JSON endpoint). Hosting: one small container on Render or Railway behind the passphrase gate. No dependency on the supplier's site for this phase.
- Frontend/backend contract: `POST /chat` with `{session_id, message}` returns `{reply, order_list?, cache_timestamp}`; conversation history is kept server-side per `session_id` in memory (or SQLite) for the life of the session and dropped after inactivity. Single response, no streaming, for P0.

## Configuration

`config/supplier.example.toml` (committed):

```toml
[supplier]
name = "HopCellar"              # placeholder, used in UI copy and logs
base_url = "https://hopcellar.example"
products_all_path = "/json/products/all"
product_by_id_path = "/json/products/id/{id}"
currency = "DKK"
prices_include_vat = true       # unconfirmed, see intent.md open questions
user_agent = "BrewChat catalog sync (personal project)"

[sync]
schedule = "daily"
cache_path = "data/catalog.sqlite"
```

`config/supplier.local.toml` (gitignored) has the same keys with the real name, domain, and base URL. Everything else in the repo, including `supplier-api.md`, should reference only placeholder values; the two example URLs in `supplier-api.md` already do, and that file can be folded into the example config once the build starts.

`.gitignore` additions needed: `config/*.local.toml`, `data/`, `logs/`. Raw catalog exports (`*_all_products_*.json`) are already excluded.

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

**Order list item** (input to `build_order_list`), one per recipe ingredient:

```json
{
  "ingredient": { "...standardized recipe ingredient..." },
  "product_handle": "string | null",
  "quantity": "number",
  "source": "matched | substituted | user_override | unavailable",
  "substitution": {
    "original_name": "string",
    "reason": "string",
    "confidence": "high | medium | low"
  }
}
```

`substitution` is present only when `source` is `substituted` or `user_override`. `unavailable` is used when the user rejected a substitution and no product is chosen; the line still appears in the list so the user knows to source it elsewhere.

**Substitution log record** (JSONL, one line per proposed substitution, written by `build_order_list`; a later build in the same session for the same original ingredient updates the decision):

```json
{
  "session_id": "random uuid, no user identity",
  "timestamp": "ISO 8601",
  "original_name": "string",
  "original_spec": "string | null",
  "proposed_handle": "string",
  "proposed_title": "string",
  "reason": "string",
  "confidence": "high | medium | low",
  "decision": "accepted | rejected | replaced_by_user | pending",
  "final_handle": "string | null",
  "cache_timestamp": "ISO 8601"
}
```

**Session timing record**: `session_id`, `started_at` (first message), `last_list_built_at`, `lists_built` (count). Used for the time-to-order measurement; the manual part of the timing is recorded by hand against the same `session_id`.

**Supplier catalog product** (real shape, confirmed from both the products-all and product-by-id endpoints, same product structure in both): the relevant fields are `Id`, `ItemNumber`, `Title` (name plus embedded spec text, e.g. "ebc 3 - 5 EBC"), `CategoryId`, `SecondaryCategoryIds`, `CategoryTitle`, `Stock` / `StockWithoutReservation`, `Soldout`, `Buyable`, `Online`, `Prices` (array with `PriceMinWithVat` / `PriceMinWithoutVat`), and `Handle` (URL slug, product link is `base_url` plus `Handle`). The cache projects down to these fields plus a `fetched_at` timestamp rather than storing the full raw response; most of the remaining fields (dates, VAT group IDs, packet IDs, etc.) aren't needed for matching or display.

**Brewing-ingredient category filter**, from a real export of the supplier's full catalog (1771 products across roughly 40 categories). Working list of `CategoryId` values that are actual brewing ingredients; everything else in the catalog is equipment, kegs, bottles, cleaning supplies, or hardware-specific accessories and should be excluded from the cache the agent searches:

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

This map is the `ingredient_type` pre-filter used by `search_catalog`. It lives in the supplier config, not in code, since category ids are shop-specific. It is a first pass from category names and counts, not confirmed with the supplier, see [intent.md](./intent.md) open questions.

## Success metrics

Ordered to match the goals in intent.md.

**Primary (goal 2, and the number the portfolio piece is built around):**
- Time from recipe in hand to order confirmed, chatbot versus my current manual process. Baseline about 2 hours per brew. Target: a clear, repeatable reduction across at least three brews, reported with the session timing log plus stopwatch for the manual cart step. The fair-measurement question (same recipe, same cart size) is open in intent.md and should be settled before the first measured run.

**Leading, during the build (days to weeks):**
- Recipe parse success rate across the fixture set: target 90%+.
- Ingredient match rate against the cached catalog: target 80%+.
- Substitution quality from the log: target 90%+ of suggestions judged reasonable on review, and a visible correlation between stated confidence and acceptance (if `high` swaps get rejected as often as `low` ones, the confidence signal isn't working).
- Adversarial prompt set: 100% redirected.

**Secondary (goal 3):**
- The supplier is interested in a one-month pilot with the chat linked from his site after a live walkthrough.
- Stop criterion, from intent.md: if he isn't, the project continues as a portfolio piece and personal tool; goal 3 is parked, not pursued further with this supplier.
- If a pilot happens: number of real orders placed by users who came through the tool, as a starting point for any collaboration or monetization conversation.

## Open questions

Business and supplier-facing open questions live in [intent.md](./intent.md). Engineering questions specific to this spec:

- Does the platform's products-all endpoint tolerate one automated fetch per day without rate limiting or blocking? Assumed yes given the size (a few MB); verify on the first scheduled run and back off if not.
- Do `Stock` and `StockWithoutReservation` differ in practice for this shop, and which one should "in stock" mean? Default to `StockWithoutReservation > 0 and Buyable and Online and not Soldout` until checked against the export.
- Is one shared passphrase enough for the private demo, or is a per-person link (token in URL) worth the small extra effort so access can be revoked individually?

Resolved since the previous version: stock is a daily snapshot by design (there is no real-time endpoint, and the staleness risk is accepted in intent.md, with the cache timestamp shown on every list).

## Timeline considerations

No hard deadline. Suggested phasing, from intent.md: 2-3 weeks to a working end-to-end demo against the cached catalog, first used for my own next brew to get a baseline-versus-chatbot time measurement, then a live walkthrough with the supplier as the opening of the pilot conversation. In build order: config plus sync job and cache first (everything depends on it), then the agent loop with the three tools against the recipe fixtures, then the substitution log and the access gate, then the chat frontend. Measurement instrumentation goes in with the list builder, not after.

## Before handover to Claude Code

Status of the review gaps, after this revision.

**Resolved in this spec:**
- Topic scoping now matches the single-agent architecture; decision made: system-prompt scoping plus restricted tools, no pre-classifier for P0.
- Matching implementation decided: category pre-filter plus `rapidfuzz` over `Title`, top 5 to the agent.
- Stack and hosting stated explicitly; frontend/backend contract written down.
- Access control promoted to P0.
- Substitution advisory guardrail promoted to P0 (reason plus confidence, plus logging).
- Output format decided for this version: copyable list with per-line links, cache timestamp, and a hand-off note. How the supplier would want to receive orders stays an intent.md question, but it no longer blocks the build since the list is the end of the flow.
- Supplier anonymization and config layout specified.

**Still to close before or during the build:**
- Recipe fixtures: 5 exist in `recipes-fixtures/recipes.md`; grow to 10, covering at least one all-grain, one extract, one with a dry-hop schedule, and one with a liquid yeast the supplier is unlikely to carry (to exercise substitution).
- Adversarial prompt fixtures don't exist yet; write them next to the recipes before the scoping tests run.
- Currency and VAT (`PriceMinWithVat` vs. without) unconfirmed. Build `build_order_list` against the `prices_include_vat` config flag so it's a one-line change either way, and show the assumption on the list until confirmed.
- The category filter needs a sanity check on edge cases (kits, cask or specialty beer) against the July export.
- The fair-measurement protocol for time-to-order (intent.md) should be written down before the first measured brew, otherwise the primary metric is soft.

## Revision notes

Changes from the previous spec, driven by the revised intent.md:

- Goals reordered and made explicit; a "Priorities that shape this spec" section added so trade-offs are traceable.
- "Supplier API" replaced throughout by the platform's unauthenticated product JSON endpoints, scraped into a daily cache. The sync job got politeness and failure-handling acceptance criteria, and the raw export stays out of the repo.
- The real-time-stock open question is closed: daily snapshot, staleness accepted, cache timestamp shown on every list.
- Substitution requirements extended: reason plus explicit confidence on every suggestion, no human-in-the-loop, user override in the session (previously P1, now P0), and an anonymous substitution log with a schema. A log review helper is P1.
- New P0 requirements: supplier anonymization via a local settings file, access gating for the private demo, and time-to-order measurement support.
- Order list item schema extended with `source` and `substitution` metadata to feed the log; `unavailable` added as a line state.
- Success metrics reordered: measured time-to-order is primary, supplier pilot is secondary with the stop criterion from intent.md.
- P2 gained supplier-driven steering and promotions (the supplier benefit described in intent.md, not yet discussed with him) and multi-shop support on the same platform as the concrete form of goal 3.
- "Before handover" reworked into resolved versus still-open, with the previously flagged topic-scoping contradiction fixed in the requirement itself.
