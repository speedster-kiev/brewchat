import subprocess
from importlib.metadata import PackageNotFoundError

import pytest

from brewchat import version
from brewchat.agent.prompt import prompt_version


@pytest.fixture(autouse=True)
def _fresh():
    version.app_version.cache_clear()
    yield
    version.app_version.cache_clear()


def test_version_is_pyproject_version_plus_commit(monkeypatch):
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    monkeypatch.setenv("BREWCHAT_GIT_COMMIT", "8f4c580")
    assert version.app_version() == "0.2.0+8f4c580"


def test_without_git_the_commit_is_left_off(monkeypatch):
    monkeypatch.delenv("BREWCHAT_GIT_COMMIT", raising=False)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")

    def no_git(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert version.app_version() == "0.2.0"


def test_missing_package_metadata_falls_back(monkeypatch):
    def missing(_name):
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(version, "version", missing)
    assert version.package_version() == version.UNKNOWN


def test_installed_version_matches_pyproject():
    import tomllib

    from brewchat.config import REPO_ROOT

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        assert version.package_version() == tomllib.load(fh)["project"]["version"]


def test_prompt_version_changes_with_the_template(tmp_path):
    doc = tmp_path / "system-prompt.md"
    doc.write_text("## The prompt\n\n```\nYou are BrewChat.\n```\n", encoding="utf-8")
    first = prompt_version(doc)
    assert len(first) == 8
    doc.write_text("## The prompt\n\n```\nYou are BrewChat, briefly.\n```\n", encoding="utf-8")
    assert prompt_version(doc) != first
