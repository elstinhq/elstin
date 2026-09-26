"""The epic integration branch: every story lands here, locally, and only this
branch ever reaches GitHub — as the one pull request the epic opens once all
of its stories have merged (forge/daemon/main.py's OpenEpicPullRequest).

A story lands in three steps, driven by the daemon's merge queue (one story
at a time per epic, see forge/daemon/main.py's `_merge_story_into_epic`):

1. `update_story_from_epic` brings the story's branch up to date: whatever
   merged into the epic branch since the story started (a story that ran in
   parallel with it) is merged into the story's own worktree. A conflict is
   left there, markers and all, for dev to resolve.
2. The daemon re-runs the Tier 1 gate on that updated worktree — so what is
   verified is exactly the code that will land, not the story alone.
3. `fast_forward_epic` moves the epic branch to the story branch, and only
   with `--ff-only`: the epic branch can never hold a tree that was not
   verified as a whole.

Every command that touches the shared clone (creating the integration
worktree, moving the epic branch) runs under forge/tools/worktree.py's
per-repo lock, since stories now run and merge concurrently.
"""
from __future__ import annotations

import os
import threading

from forge.tools.worktree import WorktreeError, _repo_lock, resolve_base_ref, run_git, slugify, ticket_ref_for


def integration_branch_name(
    epic_key: str, *, external_source: str | None = None, external_id: str | None = None
) -> str:
    """`epic/<ref>-integration`, e.g. `epic/3-integration`.

    Deliberately NOT prefixed by type the way a story branch is
    (forge/tools/worktree.py's branch_name_for): an epic aggregates several
    stories which may be a mix of features and fixes, so calling the whole
    branch `feat/` would be wrong as often as right.

    An epic that already has a branch recorded keeps it —
    `Epic.integration_branch` is the stored name and callers pass it rather
    than recomputing, so changing this format never orphans the merges an
    in-flight epic has already made."""
    return f"epic/{ticket_ref_for(epic_key, external_source, external_id)}-integration".lower()


def integration_worktree_path(worktrees_base_dir: str, epic_key: str, project_path: str | None = None) -> str:
    """Where `project_path`'s worktree of the epic branch lives.

    One per repo: an epic that spans two repos has a same-named integration
    branch in each, and each needs its own checkout. It used to be one path
    for the whole epic, so the second repo's merge ran in the FIRST repo's
    worktree and its story never landed. The first repo keeps the original
    `_integration-<epic>` path — a single-repo epic, and every epic already
    in flight, is unchanged — and any other repo gets a sibling named after
    its checkout (a sibling, not a child: a worktree nested in another shows
    up as untracked content in it)."""
    primary = os.path.realpath(os.path.join(worktrees_base_dir, f"_integration-{epic_key.lower()}"))
    if project_path is None or not os.path.isdir(primary) or _worktree_of(primary, project_path):
        return primary
    suffix = slugify(os.path.basename(os.path.realpath(project_path))) or "repo"
    return os.path.realpath(os.path.join(worktrees_base_dir, f"_integration-{epic_key.lower()}-{suffix}"))


def _worktree_of(worktree_path: str, project_path: str) -> bool:
    """True when `worktree_path` is a worktree of the repo at `project_path`."""
    theirs = run_git(worktree_path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    ours = run_git(project_path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    return (
        theirs.returncode == 0 and ours.returncode == 0
        and os.path.realpath(theirs.stdout.strip()) == os.path.realpath(ours.stdout.strip())
    )


# Choosing an integration worktree's path depends on which repo got there
# first (see integration_worktree_path), and stories in different repos
# dispatch concurrently — each holding only its own repo's lock. Choosing and
# creating the worktree therefore happen under one lock per epic.
_epic_locks: dict[str, threading.Lock] = {}
_epic_locks_guard = threading.Lock()


def _epic_lock(epic_key: str) -> threading.Lock:
    with _epic_locks_guard:
        return _epic_locks.setdefault(epic_key.lower(), threading.Lock())


def ensure_integration_worktree(
    project_path: str, epic_key: str, worktrees_base_dir: str, *, branch: str | None = None
) -> dict:
    """Create the epic's integration branch/worktree if it doesn't exist yet,
    or reopen it if it does. Returns {"branch": ..., "worktree_path": ...}.

    `branch` is the epic's recorded integration branch, passed by a caller
    that has the epic row: an epic whose merges already live on a branch
    named under an older scheme must keep using it."""
    branch = branch or integration_branch_name(epic_key)

    with _epic_lock(epic_key), _repo_lock(project_path):
        worktree_path = integration_worktree_path(worktrees_base_dir, epic_key, project_path)
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


def update_story_from_epic(
    project_path: str, epic_key: str, story_worktree: str, worktrees_base_dir: str, *, branch: str | None = None
) -> dict:
    """Merge the epic branch into the story's worktree when the epic branch has
    moved on since the story branched. Returns {"updated": bool,
    "conflicts": [path, ...], "error": str | None}.

    - Nothing new on the epic branch: {"updated": False} — the gate the story
      already passed saw exactly the code that would land.
    - A clean merge: a merge commit on the story branch, {"updated": True}.
    - A conflict: the merge is LEFT IN PROGRESS, conflict markers in the
      files, so dev can resolve them in place; the commit at the end of that
      dev run (forge/daemon/main.py) then completes the merge. Aborting would
      throw away the other stories' side of the conflict, which dev needs to
      see to reconcile the two.
    """
    try:
        integration = ensure_integration_worktree(project_path, epic_key, worktrees_base_dir, branch=branch)
    except WorktreeError as exc:
        return {"updated": False, "conflicts": [], "error": str(exc)}
    epic_branch = integration["branch"]

    if run_git(story_worktree, "merge-base", "--is-ancestor", epic_branch, "HEAD", check=False).returncode == 0:
        return {"updated": False, "conflicts": [], "error": None}

    merge = run_git(story_worktree, "merge", "--no-edit", epic_branch, check=False)
    if merge.returncode == 0:
        return {"updated": True, "conflicts": [], "error": None}
    conflicted = run_git(story_worktree, "diff", "--name-only", "--diff-filter=U", check=False)
    conflicts = sorted(line.strip() for line in conflicted.stdout.splitlines() if line.strip())
    if conflicts:
        return {"updated": True, "conflicts": conflicts, "error": None}
    # Not a content conflict (e.g. a dirty worktree refused the merge): undo
    # whatever started and report it — nothing for dev to resolve.
    run_git(story_worktree, "merge", "--abort", check=False)
    return {"updated": False, "conflicts": [], "error": merge.stderr.strip() or "merge failed"}


def fast_forward_epic(
    project_path: str, epic_key: str, story_branch: str, worktrees_base_dir: str, *, branch: str | None = None
) -> dict:
    """Move the epic branch to `story_branch`, fast-forward only. Returns
    {"applied": bool, "error": str | None}. Fails — never merges — when the
    story branch does not contain the epic branch's head, i.e. something
    landed on the epic after the story was updated and verified."""
    try:
        integration = ensure_integration_worktree(project_path, epic_key, worktrees_base_dir, branch=branch)
    except WorktreeError as exc:
        return {"applied": False, "error": str(exc)}
    with _repo_lock(project_path):
        merge = run_git(integration["worktree_path"], "merge", "--ff-only", story_branch, check=False)
    if merge.returncode != 0:
        return {"applied": False, "error": merge.stderr.strip() or "not a fast-forward"}
    return {"applied": True, "error": None}


def epic_branch_changes_since(story_worktree: str, epic_branch: str) -> list[str]:
    """Files the epic branch changed since this story branched from it — the
    other stories' side of an update, which a story sent back to fix a
    combined-tree failure may need to touch."""
    base = run_git(story_worktree, "merge-base", "HEAD", epic_branch, check=False)
    if base.returncode != 0 or not base.stdout.strip():
        return []
    diff = run_git(story_worktree, "diff", "--name-only", base.stdout.strip(), epic_branch, check=False)
    return sorted(line.strip() for line in diff.stdout.splitlines() if line.strip())
