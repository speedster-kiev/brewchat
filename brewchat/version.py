"""Which BrewChat build is running (issue #20).

The app version is the SemVer ``version`` in ``pyproject.toml`` (read from the
installed package metadata), plus the short git commit when one is known:
``0.2.0+8f4c580``. The commit comes from ``$BREWCHAT_GIT_COMMIT`` (set as a
Docker build arg, since the image has no ``.git``), else from ``git`` in the
checkout, else it is left off. The prompt version is in ``agent/prompt.py``.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

from brewchat.config import REPO_ROOT

PACKAGE = "brewchat"
UNKNOWN = "0.0.0"


def package_version() -> str:
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return UNKNOWN


def git_commit() -> str | None:
    """Short commit of the running code, or None when it can't be told."""
    env = os.environ.get("BREWCHAT_GIT_COMMIT", "").strip()
    if env:
        return env[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=2, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


@lru_cache(maxsize=1)
def app_version() -> str:
    """``<pyproject version>[+<commit>]``, computed once per process."""
    commit = git_commit()
    return f"{package_version()}+{commit}" if commit else package_version()
