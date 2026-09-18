"""Daily catalog sync. Run from host cron: `uv run scripts/sync_catalog.py`.

Exits non-zero (and keeps the previous cache) if the fetch fails or the
response changed shape.
"""

import logging
import sys

from brewchat.catalog.sync import SyncError, sync
from brewchat.config import ConfigError, load_env_file, load_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file()
    try:
        settings = load_settings()
        n = sync(settings)
    except (ConfigError, SyncError) as exc:
        logging.error("%s", exc)
        return 1
    print(f"Synced {n} ingredient products to {settings.cache_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
