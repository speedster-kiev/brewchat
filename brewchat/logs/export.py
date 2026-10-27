"""Substitution log JSONL -> CSV, one row per (session, original ingredient), last decision wins."""

from __future__ import annotations

import csv
from pathlib import Path

from brewchat.logs.substitution_log import FIELDS, SubstitutionLog


def export_csv(jsonl_path: Path, csv_path: Path) -> int:
    """Write the reduced log to ``csv_path``. Returns the number of data rows."""
    rows = SubstitutionLog(Path(jsonl_path)).read_latest()
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(FIELDS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in FIELDS})
    return len(rows)
