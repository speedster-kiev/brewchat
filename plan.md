# BrewChat P0 execution plan

## Context

`intent.md` and `spec.md` (both revised, uncommitted) define the P0 build: a single Claude agent with three tools (`submit_parsed_recipe`, `search_catalog`, `build_order_list`) that turns a pasted beer recipe into a priced, copyable shopping list from a daily-cached copy of one anonymized supplier's catalog. `system-prompt.md` already holds the prompt template; `recipes-fixtures/` holds 4 recipe fixtures (Recipe 5 is an empty header) and 17 adversarial fixtures. No code exists yet. This plan turns the spec into an ordered build with the files touched and the tests that prove each step.

Priority order from the intent applies throughout: clean architecture and docs > demo polish > supplier-specific customization.

## Three findings from the July export that adjust the spec

Checked against the local raw export (gitignored, 1771 products). These change what gets built, so they are decided here rather than discovered mid-build:

1. **`Online` is `False` on all 1771 products.** The spec's proposed in-stock rule (`StockWithoutReservation > 0 and Buyable and Online and not Soldout`) would mark everything unavailable. Decision: in-stock = `StockWithoutReservation > 0 and Buyable and not Soldout`; `Online` is projected into the cache but not used. Recorded as a resolved engineering question in `spec.md`.
2. **The spec's 11-category filter misses most of the catalog's ingredients.** It yields 234 products and excludes e.g. Pilsner malt (286), Pale ale malt (287), Munich (288), Vienna (289), Spraymalt (290), Hvede malt (36), White Labs liquid yeast (68), most hop subcategories (46, 48, 50, 51, 52, 54, 55, 103, 268), other dry yeast (78, 281), Krydderier (74), water minerals (272). Adding those gives 336 products. Decision: the category map in `config/supplier.example.toml` ships with the expanded list (still marked unconfirmed with the supplier); a `scripts/check_categories.py` prints category counts from a raw export so the map can be re-checked after each sync.
3. **`Handle` is already a full path** (`/shop/8-gaer/166-.../`), and `Link`/`Url` are `null`. `build_order_list` does `base_url.rstrip('/') + Handle`. `Prices` is always a single-element array; take `[0]`.

Also: the raw export is a dict `{amount, productIds, products, filterMap}`, not a bare list. The sync parses `products`.

## Target repository layout

```
brewchat/
  pyproject.toml                 uv-managed, Python 3.12; deps: anthropic, fastapi, uvicorn, rapidfuzz, httpx, tomli-w? (no: tomllib is stdlib), pytest, pytest-asyncio
  .gitignore                     + config/*.local.toml, data/, logs/, evals/results/
  .pre-commit-config.yaml        anonymization grep hook
  README.md                      updated status + run instructions
  config/
    supplier.example.toml        placeholders + expanded category map (committed)
    supplier.local.toml          real values (gitignored, created by hand)
  brewchat/
    __init__.py
    config.py                    load TOML, refuse to start without local file
    catalog/
      __init__.py
      schema.py                  Product dataclass (projected fields), in_stock() rule
      sync.py                    fetch products-all, project, filter, write SQLite + fetched_at
      store.py                   SQLite read side: load ingredient slice into memory, cache_timestamp()
      search.py                  category pre-filter + rapidfuzz over Title, top-5 with scores
    agent/
      __init__.py
      prompt.py                  render system-prompt.md template with config values
      tools.py                   the three @beta_tool functions
      runner.py                  tool_runner loop per session, history kept server-side
      models.py                  Pydantic models: Ingredient, OrderListItem, Substitution, OrderList
    logs/
      __init__.py
      substitution_log.py        append-only JSONL writer + decision upsert per (session, original)
      session_log.py             started_at / last_list_built_at / lists_built
      export.py                  JSONL -> CSV
    web/
      __init__.py
      app.py                     FastAPI: POST /chat, GET /, robots.txt, noindex header
      auth.py                    shared passphrase gate (env var), login form + signed cookie
      static/index.html          minimal chat page (fetch POST /chat, render list, copy button)
      sessions.py                in-memory session store with inactivity expiry
  scripts/
    sync_catalog.py              CLI entry for the daily job
    check_categories.py          category counts from a raw export (local only)
    export_logs.py               JSONL -> CSV
    run_evals.py                 recipe-parse + adversarial fixture runner, writes evals/results/
  .github/workflows/sync.yml     daily cron calling scripts/sync_catalog.py (or host cron; see step 2)
  tests/
    conftest.py                  fixtures: sample catalog (synthetic, placeholder names), tmp SQLite, config
    fixtures/catalog_sample.json synthetic products-all response, ~30 products, "HopCellar" names only
    test_config.py
    test_sync.py
    test_search.py
    test_tools.py
    test_order_list.py
    test_substitution_log.py
    test_session_log.py
    test_auth.py
    test_chat_endpoint.py
    test_anonymization.py
  evals/
    README.md                    how to run, what pass means
    results/                     gitignored; recorded replies per fixture run
  system-prompt.md               unchanged text; becomes the source file prompt.py reads
  recipes-fixtures/recipes.md    grown to 10 recipes (step 6)
  spec.md                        small edits: resolved in-stock rule, category map note
```

`supplier-api.md` is deleted once `config/supplier.example.toml` exists (spec says fold it in).

## Order of work

Each step ends with its tests green before the next starts. Steps 1-2 are the foundation the spec calls "everything depends on it".

### Step 1: Project skeleton, config, anonymization guard

Files: `pyproject.toml`, `.gitignore`, `.pre-commit-config.yaml`, `config/supplier.example.toml`, `brewchat/config.py`, `tests/test_config.py`, `tests/test_anonymization.py`, delete `supplier-api.md`.

- `config.py`: load `config/supplier.local.toml` via `tomllib`; if missing, raise a clear error naming `config/supplier.example.toml`. Expose a frozen `Settings` object: `supplier.{name, base_url, products_all_path, product_by_id_path, currency, prices_include_vat, user_agent}`, `sync.{cache_path}`, `categories` (dict `CategoryId -> ingredient_type`), `auth.passphrase` from env `BREWCHAT_PASSPHRASE`.
- `supplier.example.toml`: spec's keys plus `[categories]` table using the expanded map from finding 2, with a comment that it is unconfirmed.
- Pre-commit hook: `grep -rIn --exclude-dir=.git --exclude-dir=data --exclude-dir=logs -e '<real name>' -e '<real domain>'` must return nothing. The real strings are read from `supplier.local.toml` at hook time so they never appear in the hook config itself.

Tests:
- `test_config.py`: missing local file -> error message mentions the example path; example file parses; `categories` maps to the four ingredient types only.
- `test_anonymization.py`: walks tracked files (`git ls-files`) and asserts none contain the real name/domain read from the local config; skipped with a clear reason if the local config is absent (CI).

### Step 2: Catalog sync and cache

Files: `brewchat/catalog/schema.py`, `sync.py`, `store.py`, `scripts/sync_catalog.py`, `scripts/check_categories.py`, `.github/workflows/sync.yml`, `tests/fixtures/catalog_sample.json`, `tests/test_sync.py`.

- `schema.py`: `Product` with `id, item_number, title, category_id, secondary_category_ids, category_title, stock, stock_without_reservation, soldout, buyable, online, price_with_vat, price_without_vat, handle, ingredient_type`. `in_stock()` per finding 1. `ingredient_type` derived from `CategoryId` (fallback: first `SecondaryCategoryIds` entry in the map, since e.g. secondary category 6 "Malt" tags 112 products).
- `sync.py`: one `httpx` GET with the configured User-Agent, 30s timeout; parse `["products"]`; project + filter; write to SQLite `products` table and a `meta` table with `fetched_at`. On any exception or shape mismatch (missing `products`, zero rows after filter), log and leave the existing cache untouched, exit non-zero. Never writes the raw response to disk.
- `store.py`: `load_ingredients() -> list[Product]`, `cache_timestamp() -> datetime`.
- `.github/workflows/sync.yml`: daily cron; secrets hold the local TOML contents; cache written to a persisted artifact or, simpler, the host-side cron on the deployed container. Decision: host-side cron on the container (one place holds the secret and the SQLite file); the workflow file is a stub with instructions, not the primary path.

Tests (`test_sync.py`, using `catalog_sample.json` and `httpx.MockTransport`):
- projection keeps exactly the schema fields and drops the rest;
- filter keeps only mapped categories; counts match the fixture;
- `in_stock()` truth table: soldout, unbuyable, zero stock, ok;
- a 500 / a response without `products` / an empty filtered list each leave the previous cache file byte-identical and raise;
- `fetched_at` is written and `cache_timestamp()` reads it back.

Manual check: run `scripts/check_categories.py` against the July export and `scripts/sync_catalog.py` once against the local config; confirm ~336 rows.

### Step 3: `search_catalog`

Files: `brewchat/catalog/search.py`, `tests/test_search.py`.

- `search(query, ingredient_type=None, limit=5)`: pre-filter by type, `rapidfuzz.process.extract` with `fuzz.WRatio` over `Title` (lowercased, with a small normalizer that strips packaging noise like `11,5 g.`), return candidates `{handle, title, score, in_stock, stock, price, ingredient_type}` sorted by score. Returns `[]` explicitly when nothing scores above a floor (e.g. 40).
- Loaded once per process into memory; reload when `fetched_at` changes.

Tests:
- "US-05" with type `yeast` returns the SafAle US-05 product first;
- "Pilsner malt" with type `fermentable` never returns a hop;
- nonsense query returns an empty list;
- out-of-stock products are returned but flagged `in_stock=False` (the agent needs to see them to reason about substitutes);
- limit respected; scores descending.

### Step 4: Tools, models, order list, logs

Files: `brewchat/agent/models.py`, `tools.py`, `brewchat/logs/substitution_log.py`, `session_log.py`, `export.py`, `scripts/export_logs.py`, `tests/test_tools.py`, `test_order_list.py`, `test_substitution_log.py`, `test_session_log.py`.

- `models.py`: Pydantic models straight from spec "Data schemas": `Ingredient`, `Substitution`, `OrderListItem`, `OrderList` (items, total, currency, vat_note, cache_timestamp, handoff_note). Validation: `substitution` required iff `source in {substituted, user_override}`; `product_handle` required unless `source == unavailable`.
- `tools.py`: three `@beta_tool` functions taking the Pydantic-shaped inputs. `submit_parsed_recipe` validates and stores the parse on the session (a checkpoint, returns the count). `search_catalog` wraps step 3. `build_order_list` builds URLs (`base_url + handle`), computes the total from the configured VAT flag, stamps `cache_timestamp`, renders the plain-text list, writes substitution-log records and bumps the session log, and returns both the structured `OrderList` and the text. Tool functions get the session via a contextvar set by the runner.
- `substitution_log.py`: append JSONL to `logs/substitutions.jsonl`; a rebuild in the same session for the same `original_name` appends a new line with the updated `decision` (append-only; readers take the last line per `(session_id, original_name)`). Decision derivation: `substituted` -> `accepted`; `unavailable` with a prior proposal -> `rejected`; `user_override` -> `replaced_by_user`; records never include `base_url`.
- `session_log.py`: SQLite table `sessions(session_id, started_at, last_list_built_at, lists_built)`.
- `export.py`: JSONL -> CSV with the last-decision-wins reduction.

Tests:
- `test_order_list.py`: link = base_url + handle; total uses `PriceMinWithVat` when `prices_include_vat=true` and the other field when false; every line shows source label, price, link; text ends with the hand-off reminder; cache timestamp line present; an `unavailable` line has no link and is still listed.
- `test_substitution_log.py`: one record per substitution after a build; second build with `unavailable` for the same original produces a `rejected` record; `replaced_by_user` path; no base_url substring anywhere in the file; export CSV has one row per `(session, original)` with the final decision.
- `test_session_log.py`: first message sets `started_at` once; each build updates `last_list_built_at` and increments `lists_built`.
- `test_tools.py`: `submit_parsed_recipe` rejects a bad `type`; `build_order_list` rejects a `substituted` item without `substitution`.

### Step 5: Agent runner and system prompt rendering

Files: `brewchat/agent/prompt.py`, `runner.py`, `brewchat/web/sessions.py`, `tests/test_prompt.py`.

- `prompt.py`: read the fenced block from `system-prompt.md` (single source of truth, no copy), substitute `{{supplier_name}}`, `{{currency}}`, `{{vat_note}}`, `{{cache_timestamp}}`. Fail loudly on an unreplaced placeholder.
- `runner.py`: `run_turn(session, user_message) -> reply`. Uses `client.beta.messages.tool_runner(...)` with the three tools, `model="claude-opus-5"`, adaptive thinking (default), `max_tokens` ~16000, `tool_choice` auto, no parallel-tool disabling. Pasted recipes are wrapped by the backend in a `<recipe>` delimited block inside the user message (spec: "passed to the model as data inside a clearly delimited block"). Conversation history appended per session (`response.content` appended whole, not just text). Records `started_at` on first turn.
- `sessions.py`: dict of `session_id -> {messages, parsed_recipe, order_list, last_seen}`; sweep entries idle > 60 min.

Tests:
- `test_prompt.py`: rendered prompt contains no `{{`, contains "HopCellar" when the example config is used, and never contains `base_url`.
- Runner is exercised by the eval script (step 7) and the endpoint test (step 6) with the Anthropic client stubbed; no live API in unit tests.

### Step 6: Web layer: gate, endpoint, chat page

Files: `brewchat/web/app.py`, `auth.py`, `static/index.html`, `tests/test_auth.py`, `tests/test_chat_endpoint.py`.

- `auth.py`: `GET /login` form; `POST /login` compares with `BREWCHAT_PASSPHRASE` (constant-time), sets a signed cookie; every other route requires the cookie. `robots.txt` = disallow all; `X-Robots-Tag: noindex` on every response.
- `app.py`: `POST /chat {session_id, message}` -> `{reply, order_list?, cache_timestamp}`; `GET /` serves the page. App refuses to start without the local config (step 1) and without the passphrase env var.
- `index.html`: textarea + send, message list, order list panel with a "Copy as text" button and per-line links, cache timestamp footer. No framework.

Tests (FastAPI `TestClient`, runner stubbed to a fake that returns a canned reply / order list):
- unauthenticated `POST /chat` and `GET /` -> 401/redirect to login, body contains nothing but the login prompt;
- wrong passphrase rejected, right one sets cookie, subsequent `/chat` works;
- `/robots.txt` disallows all; `X-Robots-Tag` present;
- `/chat` response shape matches the contract, `cache_timestamp` populated;
- two messages with the same `session_id` share history; an unknown id starts a new session.

### Step 7: Fixtures and evals (live model runs)

Files: `recipes-fixtures/recipes.md` (fill Recipe 5, add 6-10), `scripts/run_evals.py`, `evals/README.md`, `evals/results/` (gitignored).

- Grow recipes to 10 per spec: at least one all-grain, one extract, one dry-hop schedule, one liquid yeast the supplier likely lacks (recipe 2's Wyeast 1056 and recipe 4's T-58 already help). Add per-recipe expected ingredient lists (type, name, amount, unit) as a YAML/JSON sidecar `recipes-fixtures/recipes.expected.json` so parse accuracy is computable.
- `run_evals.py` has two modes:
  - `parse`: run each recipe through the real agent with `search_catalog` and `build_order_list` stubbed to no-ops, capture the `submit_parsed_recipe` payload, score field accuracy against the sidecar. Pass bar: >= 90% fields correct over the set.
  - `adversarial`: run A/B/C/D fixtures per the protocol in `system-prompt.md` "Testing it against the fixtures" (B as fresh three-turn sessions; C4 with a stub catalog showing an out-of-stock hop; C with real tools against the synthetic catalog). Record tool calls made plus the full reply to `evals/results/<timestamp>.jsonl`. Pass bar: 100% A-C redirected/ignored with zero off-topic tool calls, 100% D answered.
- Both modes print a table and exit non-zero below the bar. They cost API calls and are run by hand, not in pytest.

If the adversarial run leaks, the spec's stated next step is a pre-classifier; fix the prompt first and re-run the whole set.

### Step 8: Docs, deploy, measurement

Files: `README.md`, `spec.md` (resolved questions), `Dockerfile`, `evals/README.md`.

- README: setup (`uv sync`, copy example config, set passphrase), run, sync, evals, log export. Status paragraph updated.
- Dockerfile + host cron for `sync_catalog.py`; deploy to Render/Railway with the local TOML and passphrase as secrets.
- Write the time-to-order measurement protocol (same recipe, timed from recipe in hand to order confirmed, session id recorded) into `evals/README.md` so the first measured brew has a fixed procedure.

## Verification (end to end)

1. `uv run pytest` green: all unit tests in steps 1-6, no network, no API key.
2. `uv run pre-commit run --all-files` passes the anonymization grep; also `git grep -i <real name>` returns nothing.
3. `uv run scripts/sync_catalog.py` against the real endpoint once; `sqlite3 data/catalog.sqlite 'select count(*) from products'` ~336; `select * from meta` shows today's `fetched_at`.
4. `uv run scripts/run_evals.py parse` >= 90%; `uv run scripts/run_evals.py adversarial` 100% on A-D, replies recorded.
5. `BREWCHAT_PASSPHRASE=... uv run uvicorn brewchat.web.app:app`, open `/`, get redirected to login, log in, paste Recipe 1, confirm: parsed list, substitutions with reason + confidence where a hop is missing, final list with working links (spot-check one URL in the browser), cache timestamp line, hand-off note, "Copy as text" works. Reply "keep Cascade instead of X" and confirm the rebuilt list and a `rejected`/`replaced_by_user` line in `logs/substitutions.jsonl`.
6. `uv run scripts/export_logs.py` produces a CSV with one row per proposed substitution and the final decision.

## Out of scope for this plan (P1/P2 per spec)

URL recipe input, match-confidence on direct matches, log review helper, download/email button, BeerXML, cart creation, supplier steering, multi-shop, embeddable widget.
