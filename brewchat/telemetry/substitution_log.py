"""Append-only JSONL log of substitutions and what the user finally kept.

One record per proposed substitution per ``build_order_list`` call. A later
build in the same session for the same original ingredient appends a new line
with the updated decision; readers take the last line per
``(session_id, original_name)``. Records carry only product handles, never the
shop's base URL, and no user identity beyond the random session id.

Decision derivation:
- ``substituted``   -> ``accepted``          (final_handle = proposed handle)
- ``user_override`` -> ``replaced_by_user``  (final_handle = the user's choice)
- ``unavailable`` for an ingredient that had an earlier proposal in this
  session -> ``rejected`` (final_handle = null)
- ``pending`` is reserved for sessions that ended without a final build.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brewchat.agent.models import OrderLine, OrderListItem
from brewchat.agent.session import Session

FIELDS = (
    "session_id",
    "timestamp",
    "original_name",
    "original_spec",
    "proposed_handle",
    "proposed_title",
    "reason",
    "confidence",
    "decision",
    "final_handle",
    "cache_timestamp",
)


def _find_proposal(proposals: dict[str, dict[str, str]], name: str) -> str | None:
    """Key of the prior proposal for ``name`` (case/whitespace-insensitive), if any."""
    wanted = name.strip().casefold()
    for key in proposals:
        if key.strip().casefold() == wanted:
            return key
    return None


class SubstitutionLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record_build(
        self,
        session: Session,
        items: list[OrderListItem],
        lines: list[OrderLine],
        cache_timestamp: str,
        now: datetime | None = None,
    ) -> int:
        """Write the records for one build and update ``session.proposals``. Returns records written.

        ``lines`` must be parallel to ``items`` (same order).
        """
        if len(items) != len(lines):
            raise ValueError("items and lines must be parallel")
        ts = (now or datetime.now(UTC)).isoformat()
        prior = dict(session.proposals)
        records: list[dict[str, Any]] = []
        new_proposals: dict[str, dict[str, str]] = {}

        for item, line in zip(items, lines, strict=True):
            sub = item.substitution
            base = {
                "session_id": session.session_id,
                "timestamp": ts,
                "original_spec": item.ingredient.spec,
                "cache_timestamp": cache_timestamp,
            }
            if item.source == "substituted" and sub is not None:
                handle = item.product_handle or ""
                title = line.product_title or ""
                records.append({
                    **base,
                    "original_name": sub.original_name,
                    "proposed_handle": handle,
                    "proposed_title": title,
                    "reason": sub.reason,
                    "confidence": sub.confidence,
                    "decision": "accepted",
                    "final_handle": handle,
                })
                new_proposals[sub.original_name] = {
                    "handle": handle,
                    "title": title,
                    "reason": sub.reason,
                    "confidence": sub.confidence,
                }
            elif item.source == "user_override" and sub is not None:
                key = _find_proposal(prior, sub.original_name)
                proposal = prior.get(key) if key else None
                records.append({
                    **base,
                    "original_name": key or sub.original_name,
                    "proposed_handle": proposal["handle"] if proposal else (item.product_handle or ""),
                    "proposed_title": proposal["title"] if proposal else (line.product_title or ""),
                    "reason": sub.reason,
                    "confidence": sub.confidence,
                    "decision": "replaced_by_user",
                    "final_handle": item.product_handle,
                })
            elif item.source == "unavailable":
                key = _find_proposal(prior, item.ingredient.name)
                if key is None:
                    continue  # never proposed: nothing to reject
                proposal = prior[key]
                records.append({
                    **base,
                    "original_name": key,
                    "proposed_handle": proposal["handle"],
                    "proposed_title": proposal["title"],
                    "reason": proposal.get("reason", ""),
                    "confidence": proposal.get("confidence", "low"),
                    "decision": "rejected",
                    "final_handle": None,
                })

        session.proposals.update(new_proposals)
        if records:
            self._append(records)
        return len(records)

    def _append(self, records: list[dict[str, Any]]) -> None:
        payload = "".join(
            json.dumps({k: r.get(k) for k in FIELDS}, ensure_ascii=False) + "\n" for r in records
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(payload)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def read_latest(self) -> list[dict[str, Any]]:
        """Last record per ``(session_id, original_name)``, in first-seen order."""
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for rec in self.read_all():
            latest[(rec["session_id"], rec["original_name"])] = rec
        return list(latest.values())
