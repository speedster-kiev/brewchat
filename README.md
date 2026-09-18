# BrewChat

A chat assistant that takes a pasted beer recipe, matches its ingredients against a daily-cached copy of one homebrew supplier's catalog, suggests substitutions for anything unmatched or out of stock, and returns a priced, copyable shopping list with a product link per line.

This is a side project and skill demo: an end-to-end product exercise from problem framing through an agentic architecture, built around Anthropic's agentic SDLC (intent -> spec -> build).

## Why

Homebrewers find recipes online in inconsistent formats, then have to manually cross-reference every ingredient against what their supplier actually has in stock, and hunt for substitutes themselves when something's missing. Ordering for one brew takes me about two hours. This project tests whether a chat agent can close that gap, matching a recipe straight to a real, purchasable shopping list.

## Project docs

- [`intent.md`](./intent.md): the problem, the proposed outcome, constraints, and open questions. The upstream artifact.
- [`spec.md`](./spec.md): requirements, architecture, configuration, and data schemas for the P0 build.
- [`plan.md`](./plan.md): the ordered build plan, with the findings from the catalog export that adjusted the spec.
- [`system-prompt.md`](./system-prompt.md): the agent's system prompt. The app reads the prompt straight from this file, so the doc and the running prompt cannot drift.
- [`evals/README.md`](./evals/README.md): the recipe-parse and adversarial evals, and the time-to-order measurement protocol.

## How it works

One Claude agent in a tool-use loop (Anthropic API tool runner), with exactly three tools:

| Tool | What it does |
|---|---|
| `submit_parsed_recipe` | Structured checkpoint: the agent's parse of the recipe into the standard ingredient schema, before any catalog lookup. |
| `search_catalog` | Category pre-filter plus `rapidfuzz` over product titles in the cached catalog; top 5 candidates with score, stock, and price. Also used to verify every substitute. |
| `build_order_list` | Resolves product links, computes the total, stamps the catalog timestamp, renders the copyable list, and writes the substitution and session logs. |

Substitutions are reasoned from the model's own brewing knowledge (no rule table), verified against the catalog, and always shown as suggestions with a reason and a high/medium/low confidence. Anything that must hold even if the model misbehaves (link construction, logging, timing) lives in code, not in the prompt.

```
brewchat/
  config.py          supplier config loader (refuses to start without the local file)
  catalog/           daily sync -> SQLite cache -> in-memory fuzzy search
  agent/             models, tools, system prompt rendering, tool-runner loop
  logs/              substitution log (JSONL), session timing (SQLite), CSV export
  web/               FastAPI app, passphrase gate, single-page chat UI
scripts/             sync_catalog, check_categories, check_pack_sizes, export_logs, run_evals
tests/               unit tests, no network and no API key needed
evals/               live-model evals and the measurement protocol
```

## Supplier anonymization

The supplier is called "HopCellar" (`hopcellar.example`) everywhere in this repo. The real name, domain, and endpoint base URL exist only in `config/supplier.local.toml`, which is gitignored. The model never sees the base URL: links are built inside `build_order_list`. A pre-commit hook fails any commit that contains the real name or domain, and `tests/test_anonymization.py` checks every tracked file (both read the real strings from the local config, so they never appear in the repo). Raw catalog exports, the catalog cache, and logs are all gitignored.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync
cp config/supplier.example.toml config/supplier.local.toml   # then fill in the real shop values
cp .env.example .env                                           # then set ANTHROPIC_API_KEY and BREWCHAT_PASSPHRASE
uv run pre-commit install                                      # anonymization guard
```

Set `BREWCHAT_MODEL` in `.env` to use a different Claude model (default `claude-haiku-4-5`, the cheapest; `claude-opus-5` parses and substitutes more reliably); the evals use it too, and `--model` overrides it per run. `.env` is gitignored and loaded automatically by the web app and the scripts. Variables already set in the real environment take precedence, so a host's secrets override it.

## Run

```sh
uv run scripts/sync_catalog.py                     # fetch the catalog once (one request)
uv run uvicorn brewchat.web.app:app --reload       # then open http://localhost:8000
```

The sync keeps the previous cache and exits non-zero if the fetch fails or the response changes shape. To re-check the ingredient category map against a raw export: `uv run scripts/check_categories.py path/to/export.json`. To see which pack size ("pr. 100 g.", "25 kg", "100 g") was read from each cached title, and which titles have none: `uv run scripts/check_pack_sizes.py`.

## Test and evaluate

```sh
uv run pytest                                      # unit tests, offline
uv run pre-commit run --all-files                  # anonymization guard
uv run scripts/run_evals.py parse                  # recipe parse accuracy, live model (costs API calls)
uv run scripts/run_evals.py adversarial            # scoping and injection fixtures, live model
```

See [`evals/README.md`](./evals/README.md) for pass bars and how results are recorded.

## Logs

- `logs/substitutions.jsonl`: one record per proposed substitution and the user's final decision (accepted, rejected, replaced_by_user). No user identity, no base URL.
- `logs/sessions.sqlite`: per-session `started_at`, `last_list_built_at`, `lists_built`, for the time-to-order measurement.

`uv run scripts/export_logs.py` writes the substitution log as CSV, one row per (session, original ingredient) with the final decision.

## Deploy

One small container (Render, Railway, or similar) behind the passphrase gate. The image runs the web app and a once-a-day catalog sync loop; the real supplier config is passed in as a secret, never baked into the image.

```sh
docker build -t brewchat .
docker run -p 8000:8000 \
  -e BREWCHAT_SUPPLIER_TOML="$(cat config/supplier.local.toml)" \
  -e BREWCHAT_PASSPHRASE=... -e ANTHROPIC_API_KEY=... \
  -v brewchat-data:/app/data -v brewchat-logs:/app/logs brewchat
```

The demo is private: every route except the login page requires the passphrase, `robots.txt` disallows everything, and every response carries `X-Robots-Tag: noindex`.

## Status

The P0 build from `plan.md` is done: catalog sync and cache, the three tools, the agent loop, substitution and session logging, the access-gated web chat, and the eval harness. Still open before the first measured brew: live eval runs (parse accuracy and the adversarial set), confirming the category map and VAT assumption with the supplier, and the first timed time-to-order session. Raw catalog exports are excluded from this repo since no data-use agreement is in place with the supplier yet.
