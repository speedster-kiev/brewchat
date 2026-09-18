"""Export the substitution log to CSV (last decision per session and ingredient).

    uv run scripts/export_logs.py [--jsonl logs/substitutions.jsonl] [--csv logs/substitutions.csv]

Defaults come from the configured logs directory.
"""

import argparse
import sys
from pathlib import Path

from brewchat.config import ConfigError, load_env_file, load_settings
from brewchat.logs.export import export_csv


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jsonl", type=Path, help="substitution log (default: <logs_dir>/substitutions.jsonl)")
    parser.add_argument("--csv", type=Path, help="output CSV (default: <logs_dir>/substitutions.csv)")
    args = parser.parse_args(argv)

    jsonl, out = args.jsonl, args.csv
    if jsonl is None or out is None:
        try:
            logs_dir = load_settings().logs_dir
        except ConfigError as exc:
            print(f"{exc}\nOr pass --jsonl and --csv explicitly.", file=sys.stderr)
            return 1
        jsonl = jsonl or logs_dir / "substitutions.jsonl"
        out = out or logs_dir / "substitutions.csv"

    if not jsonl.is_file():
        print(f"No substitution log at {jsonl}", file=sys.stderr)
        return 1
    n = export_csv(jsonl, out)
    print(f"Wrote {n} rows to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
