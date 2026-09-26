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

import logging
import os
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager

from forge.tools.language_hooks import ensure_dependencies

logger = logging.getLogger("forge.worktree")


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


# Conventional-commit style prefixes, chosen from the ticket's own labels
# and issue type. Labels are checked first and beat the issue type, the
# same precedence forge/services/story_kind.py uses and for the same
# reason: a label is the one signal a human set on purpose, whereas an
# issue type is often whatever the project template defaulted to. A
# GitHub issue is the clearest case — its `issue_type` is the literal
# "Issue" for everything, and `enhancement` vs `bug` lives in the labels.
_TYPE_PREFIXES: tuple[tuple[str, frozenset[str]], ...] = (
    ("fix", frozenset({"bug", "bugfix", "defect", "fix", "hotfix", "regression", "incident"})),
    ("docs", frozenset({"docs", "doc", "documentation"})),
    ("chore", frozenset({"chore", "task", "maintenance", "refactor", "tech-debt", "techdebt", "build", "ci", "infra"})),
    ("test", frozenset({"test", "tests", "qa", "testing"})),
    ("feat", frozenset({"feat", "feature", "enhancement", "improvement", "story", "epic", "new feature"})),
)

# What an unlabelled ticket with an unrecognised issue type becomes. "feat"
# rather than "chore" because the overwhelming majority of tickets reaching
# a dev story are feature work — and a branch prefix is a label for humans,
# not a routing decision, so the cost of guessing wrong is cosmetic.
DEFAULT_TYPE_PREFIX = "feat"


def branch_type_for(issue_type: str | None = None, labels: list[str] | None = None) -> str:
    """The conventional-commit prefix for a ticket: feat/fix/docs/chore/test."""
    normalized = {(label or "").strip().lower() for label in (labels or [])} - {""}
    for prefix, vocabulary in _TYPE_PREFIXES:
        if normalized & vocabulary:
            return prefix
    kind = (issue_type or "").strip().lower()
    if kind:
        for prefix, vocabulary in _TYPE_PREFIXES:
            if kind in vocabulary:
                return prefix
    return DEFAULT_TYPE_PREFIX


# `owner/repo#123` (how forge stores a GitHub issue's external_id) -> `123`.
_GITHUB_ISSUE_RE = re.compile(r"#(\d+)\s*$")


def ticket_ref_for(story_key: str, external_source: str | None = None, external_id: str | None = None) -> str:
    """The short, human-meaningful reference to put in a branch name.

    For JIRA this is the issue key itself, uppercased and kept whole —
    JIRA's development panel auto-links a branch by finding that key in the
    name, so shortening it would silently break the link back to the
    ticket. For GitHub it is the issue number, because the story key forge
    synthesises for a GitHub issue (`gh.owner.repo.3`) is an internal
    identifier that means nothing to a person reading `git branch`."""
    if (external_source or "").strip().lower() == "github" and external_id:
        match = _GITHUB_ISSUE_RE.search(str(external_id))
        if match:
            return match.group(1)
    key = str(story_key or "").strip()
    if (external_source or "").strip().lower() == "github":
        # A GitHub story whose external_id is missing or malformed: the last
        # dotted segment of `gh.owner.repo.3` is still the issue number.
        tail = key.rsplit(".", 1)[-1]
        if tail.isdigit():
            return tail
    return key.upper()


def branch_name_for(
    story_key: str,
    summary: str,
    *,
    issue_type: str | None = None,
    labels: list[str] | None = None,
    external_source: str | None = None,
    external_id: str | None = None,
) -> str:
    """`<type>/<ref>-<slug>`, e.g. `feat/3-show-the-currency-next-to-each-price`
    or `fix/ENG-123-prices-page-crashes-on-empty`.

    Nothing parses this name — forge tracks a story's branch in
    `story_worktrees.branch`, so the format is free to be readable. The one
    hard requirement is that a JIRA issue key survives intact somewhere in
    it (see ticket_ref_for).

    The type and labels are optional so that a caller which does not have
    the story row — dev_loop creating a worktree for an extra repo the plan
    named — still produces a valid name; it normally passes the branch it
    already has instead, so every repo in one story shares a branch name."""
    prefix = branch_type_for(issue_type, labels)
    ref = ticket_ref_for(story_key, external_source, external_id)
    return f"{prefix}/{ref}-{slugify(summary)}" if ref else f"{prefix}/{slugify(summary)}"


def _git(project_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", project_path, *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc


# A fetch of a healthy remote finishes in seconds; this is the ceiling for a
# stalled connection, after which we go on with the refs already in the clone.
_FETCH_TIMEOUT_SECONDS = 120


def _fetch_origin(project_path: str) -> None:
    """Best-effort `git fetch origin` — a failure never blocks the worktree.

    It runs from the daemon's own process, whose stdin is usually a live
    terminal, so it must be unable to ask anyone for anything: git would
    otherwise prompt for a password on that terminal for a private repo it
    has no credentials for, and this call — made while holding the per-repo
    lock inside the poll loop — would wait on it indefinitely, stalling
    every story. No stdin, no prompt, a timeout, and the configured GitHub
    token supplied the way a push gets it (github.com remotes only).

    A failed fetch is logged, not raised: the clone still has the refs from
    when it was made, which is what this call used to fall back on
    silently. A base that is merely stale is recoverable; a story that
    cannot start is not."""
    if not _has_origin(project_path):
        return  # an attached local repo with no remote: its own refs are all there is
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    askpass_path = None
    try:
        # Imported here, not at module load: forge.tools stays free of
        # config/service imports (see dart_tools.py for the same pattern).
        from forge.services.github_service import git_auth_env_for

        extra_env, askpass_path = git_auth_env_for(project_path)
        env.update(extra_env)
    except Exception:  # noqa: BLE001 — auth is best-effort; an unreadable config store must not block the fetch itself
        logger.warning("could not load GitHub credentials for fetch in %s", project_path, exc_info=True)

    try:
        proc = subprocess.run(
            ["git", "-C", project_path, "fetch", "origin"],
            capture_output=True, text=True, check=False, env=env,
            stdin=subprocess.DEVNULL, timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git fetch origin timed out after %ss in %s — continuing from the refs already in the clone", _FETCH_TIMEOUT_SECONDS, project_path)
        return
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

    if proc.returncode != 0:
        logger.warning(
            "git fetch origin failed in %s — continuing from the refs already in the clone: %s",
            project_path, (proc.stderr or proc.stdout or "no output").strip()[-500:],
        )


def _has_origin(project_path: str) -> bool:
    return _git(project_path, "remote", "get-url", "origin", check=False).returncode == 0


def _default_branch(project_path: str) -> str:
    """origin's default branch; for a repo without one (an attached local
    repo that was never pushed) the local main/master, else whatever is
    checked out."""
    proc = _git(project_path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        if _git(project_path, "rev-parse", "--verify", "-q", f"refs/heads/{candidate}", check=False).returncode == 0:
            return candidate
    current = _git(project_path, "symbolic-ref", "--short", "HEAD", check=False)
    return current.stdout.strip() if current.returncode == 0 and current.stdout.strip() else "main"


def _resolve_base_ref(project_path: str, base_ref: str | None) -> str:
    """Prefer the caller-supplied ref; fall back to origin/<default> if it
    doesn't exist — avoids a second round trip when the caller isn't sure
    the ref exists, e.g. a not-yet-created integration branch."""
    if base_ref:
        verify = _git(project_path, "rev-parse", "--verify", base_ref, check=False)
        if verify.returncode == 0:
            return base_ref
    default = _default_branch(project_path)
    remote = f"origin/{default}"
    if _git(project_path, "rev-parse", "--verify", "-q", remote, check=False).returncode == 0:
        return remote
    # No remote copy of it (an attached repo without origin): the local branch.
    local = _git(project_path, "rev-parse", "--verify", "-q", default, check=False)
    return default if local.returncode == 0 else "HEAD"


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
    branch: str | None = None,
    issue_type: str | None = None,
    labels: list[str] | None = None,
    external_source: str | None = None,
    external_id: str | None = None,
) -> dict:
    """`branch` overrides the computed name. A story that writes several
    repos gets one worktree per repo and they must share a branch name, so
    the caller that already knows the story's branch passes it rather than
    recomputing from a different summary in each repo."""
    # A file, not a folder, when the repo is itself a linked worktree — git
    # adds worktrees from one of those just the same.
    if not os.path.exists(os.path.join(project_path, ".git")):
        raise WorktreeError(f"Not a git repository: {project_path}")

    branch = branch or branch_name_for(
        story_key, summary,
        issue_type=issue_type, labels=labels,
        external_source=external_source, external_id=external_id,
    )
    worktree_path = resolve_worktree_path(worktrees_base_dir, story_key, repo_label)

    with _repo_lock(project_path):
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        _fetch_origin(project_path)

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


def discard_worktree(project_path: str, worktree_path: str | None, branch: str | None) -> dict:
    """Forget a worktree and its LOCAL branch: unregister and delete the
    worktree, prune, then `git branch -D`. For removing a ticket from forge —
    it never touches a remote, so a pushed copy of the branch is left alone.

    Best-effort and idempotent: a worktree already gone, an unregistered
    leftover directory, or a branch that no longer exists are all fine, and
    each failure is reported in `errors` rather than raised, so one stuck
    path does not strand the rest of a cleanup. The repo's default branch is
    never deleted, whatever it is asked to."""
    result = {"worktree_removed": False, "branch_deleted": False, "errors": []}
    have_repo = os.path.isdir(os.path.join(project_path, ".git"))

    with _repo_lock(project_path):
        if worktree_path and os.path.exists(worktree_path):
            if have_repo:
                _git(project_path, "worktree", "remove", "--force", worktree_path, check=False)
            if os.path.exists(worktree_path):  # unregistered, or the repo itself is gone
                shutil.rmtree(worktree_path, ignore_errors=True)
            result["worktree_removed"] = not os.path.exists(worktree_path)
            if not result["worktree_removed"]:
                result["errors"].append(f"could not remove the worktree at {worktree_path}")
        if not have_repo:
            if branch:
                result["errors"].append(f"{project_path} is not a git repository — branch {branch} was not deleted")
            return result

        _git(project_path, "worktree", "prune", check=False)
        if branch:
            if branch == _default_branch(project_path):
                result["errors"].append(f"refusing to delete the default branch {branch}")
            elif _branch_refs(project_path, branch)[0]:
                proc = _git(project_path, "branch", "-D", branch, check=False)
                result["branch_deleted"] = proc.returncode == 0
                if proc.returncode != 0:
                    result["errors"].append(f"could not delete branch {branch}: {proc.stderr.strip()}")
    return result


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
