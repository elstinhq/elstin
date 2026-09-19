"""Per-story worktree isolation.

Handles git worktree add -b, dirty-file sync, and cleanup, plus:

1. `{STORY-KEY}-{slug}` branch naming instead of `agent/{run_id}` (JIRA
   auto-links branches matching the issue key).
2. Richer base-ref resolution: verify a caller-supplied base_ref exists and
   fall back to the repo's default branch if not; detect whether the
   target branch already exists locally/remotely and choose the right
   `git worktree add` form (reuse local / checkout remote / new-from-base)
   instead of always creating fresh.

Also adds a per-repo lock: the reconciler deliberately dispatches every
story in a level concurrently (see forge/orchestration/reconciler.py), so
concurrent `git worktree add` calls against the same shared clone need
serializing.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager

from forge.tools.language_hooks import ensure_dependencies


class WorktreeError(RuntimeError):
    pass


_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


@contextmanager
def _repo_lock(project_path: str):
    key = os.path.realpath(project_path)
    with _repo_locks_guard:
        lock = _repo_locks.setdefault(key, threading.Lock())
    with lock:
        yield


def slugify(text: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "story"


def branch_name_for(story_key: str, summary: str) -> str:
    return f"{story_key.upper()}-{slugify(summary)}"


def _git(project_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", project_path, *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc


def _default_branch(project_path: str) -> str:
    proc = _git(project_path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def _resolve_base_ref(project_path: str, base_ref: str | None) -> str:
    """Prefer the caller-supplied ref; fall back to origin/<default> if it
    doesn't exist — avoids a second round trip when the caller isn't sure
    the ref exists, e.g. a not-yet-created integration branch."""
    if base_ref:
        verify = _git(project_path, "rev-parse", "--verify", base_ref, check=False)
        if verify.returncode == 0:
            return base_ref
    return f"origin/{_default_branch(project_path)}"


# Public aliases — forge/orchestration/integration.py needs the same git
# helpers for the epic integration branch/worktree, which isn't a
# story-keyed worktree so doesn't fit create_worktree's naming contract.
run_git = _git
default_branch = _default_branch
resolve_base_ref = _resolve_base_ref


def _branch_refs(project_path: str, branch: str) -> tuple[bool, bool]:
    proc = _git(
        project_path,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/heads/{branch}",
        f"refs/remotes/origin/{branch}",
        check=False,
    )
    refs = set(proc.stdout.splitlines())
    return f"refs/heads/{branch}" in refs, f"refs/remotes/origin/{branch}" in refs


def resolve_worktree_path(worktrees_base_dir: str, story_key: str, repo_label: str | None = None) -> str:
    """Where one story's worktree for one repo lives.

    A story that writes several repos needs a worktree in each, so the
    path nests the repo label under the story key. Nesting matters rather
    than suffixing: putting a second worktree *inside* the first one's
    directory would make it show up as untracked content in that repo's
    `git status`, and every changed-file listing forge does would pick it
    up. `repo_label=None` keeps the flat single-repo path for callers that
    predate labels."""
    story_dir = os.path.join(worktrees_base_dir, story_key.lower())
    if repo_label:
        return os.path.realpath(os.path.join(story_dir, repo_label))
    return os.path.realpath(story_dir)


def create_worktree(
    project_path: str,
    *,
    story_key: str,
    summary: str,
    worktrees_base_dir: str,
    base_ref: str | None = None,
    repo_label: str | None = None,
) -> dict:
    if not os.path.isdir(os.path.join(project_path, ".git")):
        raise WorktreeError(f"Not a git repository: {project_path}")

    branch = branch_name_for(story_key, summary)
    worktree_path = resolve_worktree_path(worktrees_base_dir, story_key, repo_label)

    with _repo_lock(project_path):
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        _git(project_path, "fetch", "origin", check=False)

        if os.path.exists(worktree_path):
            registered = _git(project_path, "worktree", "list", "--porcelain", check=False).stdout
            if f"worktree {worktree_path}\n" in registered:
                _git(project_path, "worktree", "remove", "--force", worktree_path, check=False)
            else:
                shutil.rmtree(worktree_path, ignore_errors=True)
            _git(project_path, "worktree", "prune", check=False)

        local_exists, remote_exists = _branch_refs(project_path, branch)

        if local_exists:
            _git(project_path, "worktree", "add", worktree_path, branch)
        elif remote_exists:
            _git(project_path, "worktree", "add", "-B", branch, worktree_path, f"origin/{branch}")
        else:
            resolved_base = _resolve_base_ref(project_path, base_ref)
            _git(project_path, "worktree", "add", "-b", branch, worktree_path, resolved_base)

    dependency_result = ensure_dependencies(worktree_path)
    if not dependency_result.get("passed", True):
        remove_worktree(project_path, worktree_path)
        detail = dependency_result.get("stderr") or dependency_result.get("stdout") or "dependency install failed"
        raise WorktreeError(f"Failed to prepare worktree dependencies: {detail.strip()}")

    return {
        "story_key": story_key,
        "worktree_path": worktree_path,
        "branch": branch,
        "dependencies": dependency_result,
    }


def remove_worktree(project_path: str, worktree_path: str) -> None:
    with _repo_lock(project_path):
        _git(project_path, "worktree", "remove", "--force", worktree_path, check=False)


def sync_dirty_files(project_path: str, worktree_path: str) -> None:
    """Copy uncommitted changes (modified/added/deleted/untracked) from
    project_path into worktree_path."""
    proc = _git(project_path, "status", "--porcelain", check=False)
    if proc.returncode != 0:
        return

    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        status_code = line[:2]
        rel_path = line[3:].strip()

        if " -> " in rel_path:
            old_rel, new_rel = (p.strip() for p in rel_path.split(" -> ", 1))
            old_wt_path = os.path.join(worktree_path, old_rel)
            if os.path.exists(old_wt_path):
                if os.path.isdir(old_wt_path):
                    shutil.rmtree(old_wt_path)
                else:
                    os.remove(old_wt_path)
            rel_path = new_rel

        src_file = os.path.join(project_path, rel_path)
        dest_file = os.path.join(worktree_path, rel_path)

        if os.path.isdir(src_file) and not os.path.islink(src_file):
            continue

        if "D" in status_code:
            if os.path.exists(dest_file):
                if os.path.isdir(dest_file):
                    shutil.rmtree(dest_file, ignore_errors=True)
                else:
                    os.remove(dest_file)
        elif os.path.exists(src_file):
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            if os.path.islink(src_file):
                if os.path.exists(dest_file):
                    os.remove(dest_file)
                os.symlink(os.readlink(src_file), dest_file)
            else:
                shutil.copy2(src_file, dest_file)
