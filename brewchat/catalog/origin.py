"""Country of origin for catalog products and search queries (issue #1).

A recipe that asks for "Belgian Pilsner malt" wants a malt from a Belgian
maltster. The catalog never states a country: malt titles name the maltster
("Pilsner Malt - Castle Malting, ebc 3 - 4, pr. 100 g.") and some hop titles
carry a bare country token ("Fuggles UK, 2025 pellets"). Two pieces of work
follow from that:

* ``product_origin`` derives an ISO 3166-1 alpha-2 code for a product, first
  from the producer -> country map in the supplier config (maltsters are
  shop-specific, so the list lives in config, not here), then from a country
  token in the title. ``None`` when neither says anything.
* ``style_origins`` says which origins a beer style usually calls for, from the
  ``[style_origins]`` config table; used only to rank, never to exclude.
* ``split_query_origins`` pulls origin words out of a query ("Belgian",
  "German", "UK") and hands back the rest of the query plus the codes. The
  words are noise for fuzzy matching - "Belgian Pilsner malt" scores every
  title containing "malt" alike - but a strong signal for ranking.

Codes are ISO alpha-2, so the United Kingdom is ``GB``; ``UK`` is accepted as
a spelling on the way in (in config, queries and titles) and normalised.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache

# Spellings that are not ISO 3166-1 alpha-2, accepted anywhere a code is read.
CODE_ALIASES = {"UK": "GB", "AUS": "AU", "USA": "US", "SL": "SI"}

# Bare country tokens as they appear in hop titles, e.g. "... 2025 pellets CZ,".
# Only ones seen in the catalog. "ES" is left out on purpose: it follows the
# hop name on the organic ("Økologisk") lines and is not known to mean Spain.
# A token only counts when it stands alone in upper case, so "Columbus" does
# not read as US and "US-05" is not a country (see the regex guards below).
TITLE_TOKENS = ("US", "DE", "UK", "GB", "NZ", "CZ", "AU", "AUS", "SL", "SI")

# Words a brewer writes in a query. Only unambiguous ones: bare "us", "be",
# "de" and "es" are ordinary words (and "us" is half of "US-05"), so they are
# not listed. Ireland is absent on purpose - "Irish moss" is a product, not an
# origin.
QUERY_WORDS = {
    "american": "US", "america": "US", "usa": "US",
    "german": "DE", "germany": "DE", "bavarian": "DE", "bavaria": "DE",
    "belgian": "BE", "belgium": "BE",
    "british": "GB", "english": "GB", "england": "GB", "scottish": "GB", "uk": "GB",
    "czech": "CZ", "czechia": "CZ", "bohemian": "CZ", "cz": "CZ",
    "danish": "DK", "denmark": "DK",
    "australian": "AU", "australia": "AU", "aus": "AU",
    "new zealand": "NZ", "nz": "NZ",
    "slovenian": "SI", "slovenia": "SI",
    "polish": "PL", "poland": "PL",
    "french": "FR", "france": "FR",
    "spanish": "ES", "spain": "ES",
    "japanese": "JP", "japan": "JP",
    "norwegian": "NO", "norway": "NO",
}

# A token is a country only when nothing word-like or hyphenated touches it:
# that keeps "US-05", "W-34/70" and "Columbus" out.
_TITLE_RE = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(TITLE_TOKENS, key=len, reverse=True)) + r")(?![\w-])"
)
_QUERY_RE = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(map(re.escape, QUERY_WORDS), key=len, reverse=True)) + r")(?![\w-])",
    re.IGNORECASE,
)
_HAS_CONTENT = re.compile(r"\w")


def normalize_code(value: str) -> str | None:
    """``"uk"`` -> ``"GB"``, ``"be"`` -> ``"BE"``; ``None`` if it isn't a country code."""
    code = (value or "").strip().upper()
    code = CODE_ALIASES.get(code, code)
    return code if re.fullmatch(r"[A-Z]{2}", code) else None


@lru_cache(maxsize=8)
def _producer_matcher(producers: tuple[tuple[str, str], ...]) -> tuple[re.Pattern[str], dict[str, str]]:
    """One alternation over the producer names, longest first, plus name -> code."""
    by_name = {name.lower(): code for name, code in producers}
    names = sorted(by_name, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)")
    return pattern, by_name


def product_origin(title: str, producers: Mapping[str, str] | None = None) -> str | None:
    """Country of origin for a product title: producer map first, then a title token."""
    if producers:
        pattern, by_name = _producer_matcher(tuple(producers.items()))
        m = pattern.search((title or "").lower())
        if m:
            return by_name[m.group(1)]
    m = _TITLE_RE.search(title or "")
    return normalize_code(m.group(1)) if m else None


def split_query_origins(query: str) -> tuple[str, frozenset[str]]:
    """Split ``query`` into the part to fuzzy-match and the origins it asked for.

    The origin words are removed so they don't dilute the match score. A query
    that is *only* origin words ("Belgian") keeps its text, since there would
    be nothing left to search for.
    """
    text = query or ""
    found = {QUERY_WORDS[m.group(1).lower()] for m in _QUERY_RE.finditer(text)}
    if not found:
        return text, frozenset()
    stripped = _QUERY_RE.sub(" ", text)
    if not _HAS_CONTENT.search(stripped):
        return text, frozenset(found)
    return re.sub(r"\s+", " ", stripped).strip(" ,;.-"), frozenset(found)


def style_origins(style: str | None, table: Mapping[str, tuple[str, ...]] | None) -> frozenset[str]:
    """The origins a beer style usually calls for ("Munich Helles" -> ``{"DE"}``); empty if unknown.

    An origin word in the style itself ("Czech Pale Lager", "American Pale Ale")
    wins. Otherwise the longest ``[style_origins]`` key found in the style
    decides, so "bohemian pilsner" beats "pilsner". The result only ranks
    candidates; it never removes one.
    """
    if not style or not style.strip():
        return frozenset()
    _, named = split_query_origins(style)
    if named:
        return named
    text = style.lower()
    for key in sorted((k for k in (table or {}) if k), key=len, reverse=True):
        if re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", text):
            return frozenset(table[key])
    return frozenset()


_NOT_A_PRODUCER = re.compile(r"^(?:ebc|max|pr\.?|\d)", re.IGNORECASE)


def producer_candidate(title: str) -> str | None:
    """The maltster/brand a title names, by its shape: "<product> - <producer>, <spec>, ...".

    A heuristic for ``scripts/check_origins.py`` only (which producers still
    lack an ``[origins]`` entry); ranking uses the config map, not this.
    """
    _, sep, rest = (title or "").partition(" - ")
    name = rest.split(",")[0].strip() if sep else ""
    return name if name and not _NOT_A_PRODUCER.match(name) else None
