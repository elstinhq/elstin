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


# --- attach: a repo already on disk, used in place ---------------------------

from forge.services.repo_clone import RepoAttachError, attach_local_repository  # noqa: E402


def _local_repo(path, *, commit: bool = True) -> str:
    path.mkdir(parents=True)
    _run_git(["git", "init", "-b", "main", str(path)], str(path))
    if commit:
        _run_git(["git", "config", "user.email", "test@example.com"], str(path))
        _run_git(["git", "config", "user.name", "Test"], str(path))
        (path / "README.md").write_text("hi\n")
        _run_git(["git", "add", "README.md"], str(path))
        _run_git(["git", "commit", "-m", "initial"], str(path))
    return str(path)


def test_attach_returns_the_resolved_repo_folder(tmp_path):
    repo = _local_repo(tmp_path / "app")
    link = tmp_path / "link-to-app"
    link.symlink_to(repo)

    assert attach_local_repository(str(link)) == str((tmp_path / "app").resolve())


def test_attach_expands_a_home_relative_path(tmp_path, monkeypatch):
    _local_repo(tmp_path / "code" / "app")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert attach_local_repository("~/code/app") == str((tmp_path / "code" / "app").resolve())


@pytest.mark.parametrize(
    "setup,fragment",
    [
        (lambda tmp: "", "Folder path is required"),
        (lambda tmp: "relative/app", "Give the full path"),
        (lambda tmp: str(tmp / "missing"), "Only folders mounted into the app container are visible"),
        (lambda tmp: (tmp / "plain").mkdir() or str(tmp / "plain"), "is not a git repository"),
        (lambda tmp: _local_repo(tmp / "empty", commit=False), "has no commits yet"),
    ],
)
def test_attach_refuses_what_forge_cannot_work_from(tmp_path, setup, fragment):
    with pytest.raises(RepoAttachError, match=fragment):
        attach_local_repository(setup(tmp_path))


def test_attach_refuses_a_subfolder_and_names_the_repo_to_attach(tmp_path):
    repo = _local_repo(tmp_path / "app")
    (tmp_path / "app" / "src").mkdir()

    with pytest.raises(RepoAttachError, match=f"inside the git repository at {repo}"):
        attach_local_repository(str(tmp_path / "app" / "src"))


def test_attach_refuses_forges_own_worktrees(tmp_path):
    _local_repo(tmp_path / "worktrees" / "eng-2")

    with pytest.raises(RepoAttachError, match="forge's own working folder"):
        attach_local_repository(str(tmp_path / "worktrees" / "eng-2"), forbidden_dirs=[str(tmp_path / "worktrees")])


# --- a folder holding repos, not being one -----------------------------------

from forge.services.repo_clone import scan_folder  # noqa: E402


def _workspace(tmp_path):
    """Like ../mkt: a plain folder with a checkout per service beside other
    things that are not repos."""
    root = tmp_path / "workspace"
    _local_repo(root / "mkt-apiservice")
    _local_repo(root / "Mkt-adminportal")
    _local_repo(root / "fresh", commit=False)
    (root / "docs").mkdir()
    (root / "notes.txt").write_text("x\n")
    _local_repo(root / ".hidden-repo")
    _local_repo(root / "docs" / "nested-repo")  # one level only
    return root


def test_a_folder_holding_repos_lists_them_to_pick_from(tmp_path):
    root = _workspace(tmp_path)

    result = scan_folder(str(root))

    assert result["path"] == str(root.resolve()) and result["is_repo"] is False
    assert [(r["name"], r["problem"] is None) for r in result["repos"]] == [
        ("fresh", False), ("Mkt-adminportal", True), ("mkt-apiservice", True),
    ]
    assert result["repos"][0]["problem"].startswith("has no commits yet")  # the row already names the repo
    assert result["repos"][2]["path"] == str((root / "mkt-apiservice").resolve())


def test_a_repo_scans_as_itself(tmp_path):
    repo = _local_repo(tmp_path / "app")

    assert scan_folder(repo) == {"path": str((tmp_path / "app").resolve()), "is_repo": True, "repos": []}
    with pytest.raises(RepoAttachError, match="inside the git repository"):
        (tmp_path / "app" / "src").mkdir()
        scan_folder(str(tmp_path / "app" / "src"))


def test_attaching_a_folder_that_holds_repos_names_them(tmp_path):
    root = _workspace(tmp_path)

    with pytest.raises(RepoAttachError, match=r"not a git repository, but it holds 3: fresh, Mkt-adminportal, mkt-apiservice"):
        attach_local_repository(str(root))
    (root / "empty").mkdir()
    with pytest.raises(RepoAttachError, match="run `git init` there"):
        attach_local_repository(str(root / "empty"))


# --- browsing, for the folder picker ------------------------------------------

from forge.services.repo_clone import browse_folder  # noqa: E402


def test_browsing_lists_subfolders_and_marks_repos(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    (root / "notes.txt").write_text("x\n")

    listing = browse_folder(str(root))

    assert listing["path"] == str(root.resolve()) and listing["parent"] == str(root.resolve().parent)
    assert listing["is_repo"] is False and listing["truncated"] is False
    assert [(e["name"], e["is_repo"]) for e in listing["entries"]] == [
        ("docs", False), ("fresh", True), ("Mkt-adminportal", True), ("mkt-apiservice", True),
    ]  # folders only, hidden ones skipped, case-insensitive order
    inside = browse_folder(listing["entries"][2]["path"])
    assert inside["is_repo"] is True

    monkeypatch.setenv("HOME", str(root))
    assert browse_folder(None)["path"] == str(root.resolve())
    assert browse_folder("/")["parent"] is None
    with pytest.raises(RepoAttachError, match="No folder at"):
        browse_folder(str(root / "nope"))
    with pytest.raises(RepoAttachError, match="full path"):
        browse_folder("relative/path")
