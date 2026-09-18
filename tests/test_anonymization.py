"""Tracked files must never contain the real supplier name or domain.

The real strings are read from the gitignored local config, so this test is
skipped where that file does not exist (CI, fresh clones).
"""

import subprocess
from urllib.parse import urlparse

import pytest

from brewchat.config import LOCAL_CONFIG, REPO_ROOT, load_settings


def _needles() -> list[str]:
    s = load_settings(LOCAL_CONFIG)
    host = urlparse(s.supplier.base_url).hostname or ""
    needles = {s.supplier.name.lower(), host.lower(), host.lower().removeprefix("www.")}
    return [n for n in needles if n and "hopcellar" not in n]


@pytest.mark.skipif(not LOCAL_CONFIG.is_file(), reason="no config/supplier.local.toml (real values absent)")
def test_tracked_files_do_not_contain_real_supplier():
    needles = _needles()
    assert needles, "local config still holds placeholder values"
    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode().split("\0")
    leaks = []
    for rel in filter(None, files):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore").lower()
        leaks += [f"{rel}: {n}" for n in needles if n in text]
    assert not leaks, f"real supplier identity in tracked files: {leaks}"
