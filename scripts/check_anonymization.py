"""Pre-commit guard: fail if staged/tracked files contain the real supplier name or domain.

The real strings are read from the gitignored local config at hook time, so
they never appear in the hook configuration itself. No local config means
nothing to check against (fresh clone, CI), and the hook passes.
"""

import sys
from pathlib import Path
from urllib.parse import urlparse

from brewchat.config import LOCAL_CONFIG, load_settings


def main(paths: list[str]) -> int:
    if not LOCAL_CONFIG.is_file():
        return 0
    s = load_settings(LOCAL_CONFIG)
    host = (urlparse(s.supplier.base_url).hostname or "").lower()
    needles = {n for n in {s.supplier.name.lower(), host, host.removeprefix("www.")} if n}
    bad = 0
    for rel in paths:
        p = Path(rel)
        if not p.is_file() or p.resolve() == LOCAL_CONFIG.resolve():
            continue
        text = p.read_bytes().decode("utf-8", errors="ignore").lower()
        for n in needles:
            if n in text:
                # Print the file, not the matched string, so the hook output stays clean.
                print(f"{rel}: contains the real supplier identity")
                bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
