"""commit_all against a real git repo — no database needed."""
from __future__ import annotations

import subprocess

import pytest

from forge.tools import git_tools


def _run(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def test_commit_all_succeeds_without_any_git_identity_configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Regression: a freshly cloned repo (forge/services/repo_clone.py)
    carries no user.name/user.email, and the host running forge may have
    no global git identity either — commit_all used to fail with "Author
    identity unknown" in exactly that situation. HOME is repointed at an
    empty directory so this test can't accidentally pass by picking up the
    real machine's global git config."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    (tmp_path / "empty_home").mkdir()

    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], str(repo))
    (repo / "README.md").write_text("hello\n")

    sha = git_tools.commit_all(str(repo), "initial import")

    assert sha is not None
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True, check=True)
    assert "initial import" in log.stdout


def test_commit_all_returns_head_when_nothing_to_commit(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    (tmp_path / "empty_home").mkdir()

    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], str(repo))
    (repo / "README.md").write_text("hello\n")
    first_sha = git_tools.commit_all(str(repo), "initial import")

    second_sha = git_tools.commit_all(str(repo), "nothing changed")

    assert second_sha == first_sha


def _init_repo_with_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    (tmp_path / "empty_home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], str(repo))
    (repo / "README.md").write_text("hello\n")
    return repo


def _head(repo, fmt: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), "log", "-1", f"--format={fmt}"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_commit_all_credits_the_given_author_and_names_code_forge_as_co_author(tmp_path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_repo_with_file(tmp_path, monkeypatch)

    git_tools.commit_all(str(repo), "ENG-1: add readme", author=("Leo Elstin", "leo@example.com"))

    assert _head(repo, "%an <%ae>") == "Leo Elstin <leo@example.com>"
    assert _head(repo, "%B").endswith("Co-authored-by: code_forge <forge@localhost>")
    assert _head(repo, "%s") == "ENG-1: add readme"


def test_commit_all_without_an_author_is_code_forge_and_adds_no_redundant_trailer(tmp_path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_repo_with_file(tmp_path, monkeypatch)

    git_tools.commit_all(str(repo), "ENG-1: add readme")

    assert _head(repo, "%an <%ae>") == "code_forge <forge@localhost>"
    assert "Co-authored-by" not in _head(repo, "%B")
