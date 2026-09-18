# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

BrewChat turns a pasted beer recipe into a priced, copyable shopping list from a daily-cached copy of one homebrew supplier's catalog. It is one Claude agent (Anthropic tool runner; model from `BREWCHAT_MODEL`, default `claude-haiku-4-5` in `config.DEFAULT_MODEL`; `runner.py` sends adaptive thinking and refusal fallbacks only to models that accept them) with exactly three tools. Read `intent.md` → `spec.md` → `plan.md` for the why; the spec's priority order applies to trade-offs: clean architecture and docs > demo polish > supplier-specific customization.

## Commands

```sh
uv sync                                              # Python 3.12, deps + dev group
uv run pytest                                        # full offline suite (no network, no API key)
uv run pytest tests/test_order_list.py::test_name    # single test
uv run pre-commit run --all-files                    # supplier anonymization guard
uv run scripts/sync_catalog.py                       # fetch catalog -> data/catalog.sqlite (needs local config)
uv run scripts/check_categories.py <raw export.json> # per-category counts vs the category map
uv run scripts/check_pack_sizes.py                   # pack sizes parsed from cached titles
uv run scripts/export_logs.py                        # substitution log JSONL -> CSV
uv run uvicorn brewchat.web.app:app
uv run python scripts/run_evals.py parse|adversarial [--only ID] [--judge]   # live API, by hand only
```

Env vars are read from a gitignored `.env` (template `.env.example`), loaded by `config.load_env_file()` in the entry points only (never at import, so tests don't see local secrets; real env wins; `BREWCHAT_ENV_FILE=""` disables). Vars: `BREWCHAT_CONFIG` (config path override), `BREWCHAT_LOGS_DIR`, `BREWCHAT_PASSPHRASE` (required by the web app), `BREWCHAT_SECRET_KEY` (cookie signing, else derived from passphrase), `BREWCHAT_MODEL` (agent + evals model; `Settings.model`, `run_evals.py --model` overrides), `ANTHROPIC_API_KEY`.

## Supplier anonymization (hard requirement)

The real supplier is called "HopCellar" / `hopcellar.example` everywhere in code, docs, tests, fixtures, logs and commit messages. The real name, domain and base URL exist only in the gitignored `config/supplier.local.toml`. Never write them anywhere else, and don't read or copy from the gitignored raw export (`*_all_products_*.json` in the repo root; even its filename is identifying). The pre-commit hook and `tests/test_anonymization.py` read the real strings from the local config at run time and fail on any match; both are no-ops without that file.

Related invariants: the model never sees `base_url` (links are built in `build_order_list`, `render_system_prompt` raises if the URL leaks into the prompt), and logs/error messages carry product `Handle`s only, never full URLs.

## Architecture

Data flow: `catalog/sync.py` (one GET to the platform's products-all endpoint, project + category-filter, atomic write of a fresh SQLite file; on any failure the previous cache stays byte-identical) → `catalog/store.py` → `catalog/search.py` `CatalogIndex` (in-memory, reloads when `fetched_at` changes; rapidfuzz `WRatio` over normalized titles; returns out-of-stock candidates flagged, not hidden).

Agent: `agent/runner.py` `Runner.run_turn(session, message)` renders the system prompt, wraps pasted recipe text in `<recipe>…</recipe>` (heuristic in `wrap_user_message`), and drives `client.beta.messages.tool_runner`. History lives in `Session.messages` as full content blocks (thinking blocks intact) and is only committed after a successful turn. Tools (`agent/tools.py`, built by `make_tools(ToolContext)`) get the session through the `current_session` ContextVar in `agent/session.py`, not as a model-supplied argument. Pydantic validation errors are re-raised as `ToolError` so the model sees which field was wrong.

- `submit_parsed_recipe`: structured checkpoint, stores `session.parsed_recipe`.
- `search_catalog`: wraps `CatalogIndex.search`.
- `build_order_list`: resolves links, prices by the `prices_include_vat` flag, stamps the cache timestamp, renders the plain-text list, sets `session.order_list`, writes the substitution log and session timing log. Anything that must hold even if the model misbehaves (links, logging, timing) belongs in code here, not in the prompt.

Logs (`logs/`): `substitutions.jsonl` is append-only; a rebuild appends a new record and readers take the last line per `(session_id, original_name)`. Decisions are derived from `session.proposals` (substituted → accepted, user_override → replaced_by_user, unavailable after a prior proposal → rejected). `sessions.sqlite` holds `started_at` / `last_list_built_at` / `lists_built` for the time-to-order metric.

Web (`web/`): `create_app(settings, runner)`; module-level `app` is created lazily via module `__getattr__` so tests can import without a local config. A passphrase gate middleware protects everything except `/login` and `/robots.txt`. `POST /chat {session_id?, message}` → `{session_id, reply, order_list, cache_timestamp}`, runner executed in a threadpool under a per-session lock; errors map to short JSON messages in `_map_runner_error`. `web/static/index.html` is a single framework-free page.

## Things that are easy to get wrong

- `system-prompt.md` is the runtime source of the prompt: `agent/prompt.py` reads the fenced block under "## The prompt" and fills `{{supplier_name}}`, `{{currency}}`, `{{vat_note}}`, `{{cache_timestamp}}`. Editing that doc changes the live prompt; re-run the whole adversarial eval set after any prompt change, not just the failing fixture.
- Shop specifics (category ids → ingredient type, exclusions, endpoints, VAT flag) live in `config/supplier.example.toml` / the local TOML, not code. Filtering: primary `CategoryId` mapped, else a mapped secondary category unless the primary is in `exclude_categories`.
- Pack size lives in the title ("pr. 100 g.", "pr. 25 kg.", "100 g"), parsed by `catalog/pack.py` (`Product.pack_size()`); the price is per one such unit. `build_order_list` recomputes `quantity` from the recipe amount whenever the units are comparable (mass/volume, via `brewchat/units.py`, shared with the eval scorer) and only trusts the model's quantity for per-pack items. `search.normalize()` strips the same sizes for scoring only. `uv run scripts/check_pack_sizes.py` audits the parse against the real cache.
- In-stock = `StockWithoutReservation > 0 and Buyable and not Soldout`. `Online` is deliberately ignored (false on every real product). `Handle` is already a full path, so link = `base_url.rstrip('/') + handle`; `Prices` is a single-element list.
- `OrderList.cache_timestamp` / `TurnResult.cache_timestamp` are ISO 8601; the human-readable sentence is rendered separately.
- Unit tests use the synthetic catalog `tests/fixtures/catalog_sample.json` via `conftest.py` fixtures (`settings`, `catalog_cache`) with tmp paths; the Anthropic client is always faked. Evals (`brewchat/evals.py` holds the pure scoring/verdict logic) also run against the synthetic catalog, so they never need the real config. Scoring conventions are in `evals/README.md`.
- Deploy: `Dockerfile` + `docker/entrypoint.sh` write the local TOML from `BREWCHAT_SUPPLIER_TOML`, sync on first boot, then re-sync daily in a background loop.
