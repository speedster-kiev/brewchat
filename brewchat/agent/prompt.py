"""System prompt rendering.

The prompt text lives in ``system-prompt.md`` (the fenced block under
"## The prompt") and is read at runtime, so that document stays the single
source of truth. Only four placeholders are filled; the supplier base URL is
deliberately never available to the template.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from brewchat.config import REPO_ROOT, Settings

SYSTEM_PROMPT_DOC = REPO_ROOT / "system-prompt.md"

_SECTION_RE = re.compile(r"^## The prompt\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]*)\s*\}\}")
_LEFTOVER_RE = re.compile(r"\{\{|\}\}")


class PromptError(RuntimeError):
    """Raised when the prompt template is missing, malformed, or left unfilled."""


@lru_cache(maxsize=4)
def _load_template_cached(path: str, mtime_ns: int) -> str:
    text = Path(path).read_text(encoding="utf-8")
    section = _SECTION_RE.search(text)
    if section is None:
        raise PromptError(f"{Path(path).name}: no '## The prompt' section found")
    fence = _FENCE_RE.search(text, section.end())
    if fence is None:
        raise PromptError(f"{Path(path).name}: no fenced block under '## The prompt'")
    return fence.group(1).strip("\n")


def load_template(path: Path = SYSTEM_PROMPT_DOC) -> str:
    """Return the raw prompt template (re-read whenever the file changes)."""
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError as exc:
        raise PromptError(f"System prompt document not found: {path.name}") from exc
    return _load_template_cached(str(path), mtime)


def format_cache_timestamp(ts: datetime | str) -> str:
    """Human-readable, stable rendering of the catalog refresh time."""
    if isinstance(ts, str):
        return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def render_system_prompt(
    settings: Settings, cache_timestamp: datetime | str, *, path: Path = SYSTEM_PROMPT_DOC
) -> str:
    template = load_template(path)
    values = {
        "supplier_name": settings.supplier.name,
        "currency": settings.supplier.currency,
        "vat_note": settings.supplier.vat_note,
        "cache_timestamp": format_cache_timestamp(cache_timestamp),
    }
    unknown: set[str] = set()

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        unknown.add(key or "<empty>")
        return m.group(0)

    rendered = _PLACEHOLDER_RE.sub(_sub, template)
    if unknown:
        raise PromptError(f"Unreplaced placeholders in system prompt: {sorted(unknown)}")
    if _LEFTOVER_RE.search(rendered):
        raise PromptError("System prompt still contains '{{' or '}}' after rendering")
    if settings.supplier.base_url and settings.supplier.base_url.rstrip("/") in rendered:
        raise PromptError("System prompt must never contain the supplier base_url")
    return rendered
