#!/bin/sh
# Writes the supplier config from the environment, keeps the catalog cache
# fresh with a once-a-day sync loop (the "host-side cron" from plan.md step 2),
# and runs the web app in the foreground.
set -eu

if [ -n "${BREWCHAT_SUPPLIER_TOML:-}" ]; then
  printf '%s\n' "$BREWCHAT_SUPPLIER_TOML" > config/supplier.local.toml
fi

sync_once() {
  uv run --no-dev python scripts/sync_catalog.py || echo "catalog sync failed; keeping previous cache" >&2
}

# First boot: make sure a cache exists before serving.
[ -f data/catalog.sqlite ] || sync_once

# Daily sync. A failed sync keeps the previous cache (see brewchat/catalog/sync.py).
( while true; do sleep 86400; sync_once; done ) &

exec uv run --no-dev uvicorn brewchat.web.app:app --host 0.0.0.0 --port "${PORT:-8000}"
