"""forge/services/repo_clone.py — clones from a local bare repo so this
stays fast and network-free, same reasoning as conftest.py's git_repo
fixture."""
from __future__ import annotations

import subprocess

import pytest

from forge.services.repo_clone import RepoCloneError, _derive_repo_name, clone_repository


def _run_git(args: list[str], cwd: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def bare_repo_url(tmp_path) -> str:
    bare = tmp_path / "widgets.git"
    _run_git(["git", "init", "--bare", "-b", "main", str(bare)], str(tmp_path))

    seed = tmp_path / "seed"
    _run_git(["git", "clone", str(bare), str(seed)], str(tmp_path))
    _run_git(["git", "config", "user.email", "test@example.com"], str(seed))
    _run_git(["git", "config", "user.name", "Test"], str(seed))
    (seed / "README.md").write_text("hello\n")
    _run_git(["git", "add", "README.md"], str(seed))
    _run_git(["git", "commit", "-m", "initial"], str(seed))
    _run_git(["git", "push", "origin", "main"], str(seed))
    return str(bare)


def test_clone_repository_creates_checkout(tmp_path, bare_repo_url):
    dest_base = tmp_path / "dest"
    path = clone_repository(bare_repo_url, dest_base_dir=str(dest_base))
    assert path == str(dest_base / "widgets")
    assert (dest_base / "widgets" / ".git").is_dir()
    assert (dest_base / "widgets" / "README.md").is_file()


def test_clone_repository_dedupes_name_on_collision(tmp_path, bare_repo_url):
    dest_base = tmp_path / "dest"
    first = clone_repository(bare_repo_url, dest_base_dir=str(dest_base))
    second = clone_repository(bare_repo_url, dest_base_dir=str(dest_base))
    assert first != second
    assert second == str(dest_base / "widgets-2")


def test_clone_repository_empty_url_raises(tmp_path):
    with pytest.raises(RepoCloneError):
        clone_repository("   ", dest_base_dir=str(tmp_path / "dest"))


def test_clone_repository_unwritable_dest_base_raises_clean_error(tmp_path, bare_repo_url):
    # A regular file where a directory component is expected makes
    # os.makedirs raise NotADirectoryError — stands in for the real-world
    # case (dest_base_dir pointing at a Docker-only path like /data/... on
    # a host that doesn't have it) without needing root/chmod tricks.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")

    with pytest.raises(RepoCloneError, match="Could not create"):
        clone_repository(bare_repo_url, dest_base_dir=str(blocker / "dest"))


def test_clone_repository_bad_source_raises_and_cleans_up(tmp_path):
    dest_base = tmp_path / "dest"
    with pytest.raises(RepoCloneError):
        clone_repository(str(tmp_path / "does-not-exist"), dest_base_dir=str(dest_base))
    # A failed clone must not leave a half-populated directory behind for
    # the next attempt (or _unique_dest_path) to trip over.
    assert list(dest_base.glob("*")) == [] if dest_base.exists() else True


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:owner/repo.git", "repo"),
        ("https://github.com/owner/repo", "repo"),
        ("https://github.com/owner/repo.git", "repo"),
        ("https://github.com/owner/repo/", "repo"),
    ],
)
def test_derive_repo_name(url, expected):
    assert _derive_repo_name(url) == expected
