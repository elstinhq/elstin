"""The epic-level integration branch: completed stories in a level get
merged here before the next level starts (`_create_integration_worktree` /
`_merge_child_into_integration`) — the mechanism the reconciler's
`MergeIntegrationBranch` intent triggers.

Phase 1 scope: the merge happens against the local shared clone's branches
directly (story branches created by forge/tools/worktree.py never leave the
local clone yet — there's no GitHub MCP server pushing/opening real PRs
until Phase 2), so this merges `story_branch` straight in, no `origin/`
fetch needed for it specifically.
"""
from __future__ import annotations

import os

from forge.tools.worktree import WorktreeError, resolve_base_ref, run_git


def integration_branch_name(epic_key: str) -> str:
    return f"agent/epic-{epic_key.lower()}"


def _integration_worktree_path(worktrees_base_dir: str, epic_key: str) -> str:
    return os.path.realpath(os.path.join(worktrees_base_dir, f"_integration-{epic_key.lower()}"))


def ensure_integration_worktree(project_path: str, epic_key: str, worktrees_base_dir: str) -> dict:
    """Create the epic's integration branch/worktree if it doesn't exist yet,
    or reopen it if it does. Returns {"branch": ..., "worktree_path": ...}."""
    branch = integration_branch_name(epic_key)
    worktree_path = _integration_worktree_path(worktrees_base_dir, epic_key)

    if os.path.isdir(worktree_path):
        return {"branch": branch, "worktree_path": worktree_path}

    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    local_exists = run_git(project_path, "rev-parse", "--verify", branch, check=False).returncode == 0
    if local_exists:
        run_git(project_path, "worktree", "add", worktree_path, branch)
    else:
        base = resolve_base_ref(project_path, None)
        run_git(project_path, "worktree", "add", "-b", branch, worktree_path, base)

    return {"branch": branch, "worktree_path": worktree_path}


def merge_story_into_integration(project_path: str, epic_key: str, story_branch: str, worktrees_base_dir: str) -> dict:
    """Merge story_branch into the epic's integration branch. Returns
    {"applied": bool, "error": str | None}."""
    try:
        integration = ensure_integration_worktree(project_path, epic_key, worktrees_base_dir)
    except WorktreeError as exc:
        return {"applied": False, "error": str(exc)}

    worktree_path = integration["worktree_path"]
    merge = run_git(worktree_path, "merge", "--no-edit", story_branch, check=False)
    if merge.returncode != 0:
        run_git(worktree_path, "merge", "--abort", check=False)
        return {"applied": False, "error": merge.stderr.strip() or "merge conflict"}
    return {"applied": True, "error": None}
