"""No database needed — exercises real git against a throwaway repo in a
temp dir, confirming branch naming, worktree creation/removal, and the
per-language dependency hook's "nothing recognized" path all work.
`git_repo` fixture lives in conftest.py (shared with test_daemon.py)."""
from __future__ import annotations

import pytest

from forge.tools.worktree import branch_name_for, create_worktree, remove_worktree, slugify


def test_slugify_and_branch_naming():
    assert slugify("Add a Widget!! To The Cart") == "add-a-widget-to-the-cart"
    assert branch_name_for("eng-42", "Add a widget") == "ENG-42-add-a-widget"


def test_create_and_remove_worktree(git_repo, tmp_path):
    worktrees_dir = str(tmp_path / "worktrees")
    result = create_worktree(
        git_repo, story_key="ENG-2", summary="Add a widget", worktrees_base_dir=worktrees_dir
    )

    assert result["branch"] == "ENG-2-add-a-widget"
    assert result["story_key"] == "ENG-2"
    import os

    assert os.path.isdir(result["worktree_path"])
    assert os.path.isfile(os.path.join(result["worktree_path"], "README.md"))
    assert result["dependencies"]["skipped"] is True  # no pom.xml/pubspec.yaml/etc in this fixture repo

    remove_worktree(git_repo, result["worktree_path"])
    assert not os.path.isdir(result["worktree_path"])


def test_create_worktree_reuses_existing_local_branch(git_repo, tmp_path):
    worktrees_dir = str(tmp_path / "worktrees")
    first = create_worktree(git_repo, story_key="ENG-3", summary="Retry me", worktrees_base_dir=worktrees_dir)
    remove_worktree(git_repo, first["worktree_path"])

    # branch ENG-3-retry-me still exists locally after the worktree was removed —
    # a second create_worktree call for the same story should reuse it, not fail.
    second = create_worktree(git_repo, story_key="ENG-3", summary="Retry me", worktrees_base_dir=worktrees_dir)
    assert second["branch"] == first["branch"]


def test_create_worktree_rejects_non_git_directory(tmp_path):
    from forge.tools.worktree import WorktreeError

    with pytest.raises(WorktreeError):
        create_worktree(
            str(tmp_path), story_key="ENG-4", summary="x", worktrees_base_dir=str(tmp_path / "wt")
        )
