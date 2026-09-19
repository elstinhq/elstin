"""Git-backed tools for the dev/verifier loop. `explore_project()`-style
whole-project exploration is intentionally not included — nothing in
forge's SDK-only dev loop calls it, and building a whole separate
project-exploration subsystem for that isn't worth it.
"""
import os
import subprocess


class GitMergeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "merge_failed",
        conflict_files: list[str] | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.conflict_files = conflict_files or []
        self.stderr = stderr


def _run_git(args: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)


def get_checkout_branch(repo_path: str) -> str:
    proc = _run_git(["branch", "--show-current"], cwd=repo_path)
    if proc.returncode != 0:
        raise GitMergeError(proc.stderr.strip() or "Could not read checkout branch", code="branch_read_failed", stderr=proc.stderr)
    branch = proc.stdout.strip()
    if not branch:
        raise GitMergeError("Detached HEAD — checkout a branch before merging", code="detached_head")
    return branch


def is_repo_clean(repo_path: str) -> bool:
    proc = _run_git(["status", "--porcelain", "--untracked-files=no"], cwd=repo_path)
    if proc.returncode != 0:
        return False
    return not proc.stdout.strip()


def merge_in_progress(repo_path: str) -> bool:
    proc = _run_git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=repo_path)
    return proc.returncode == 0


def abort_merge_if_in_progress(repo_path: str) -> None:
    if merge_in_progress(repo_path):
        proc = _run_git(["merge", "--abort"], cwd=repo_path)
        if proc.returncode != 0:
            raise GitMergeError(proc.stderr.strip() or "Failed to abort in-progress merge", code="merge_abort_failed", stderr=proc.stderr)


# A freshly cloned repo (forge/services/repo_clone.py) carries no
# user.name/user.email, and there's no guarantee the host running forge has
# a global git identity configured either ("Author identity unknown" is the
# commit failure otherwise) — passed as per-invocation `-c` overrides
# rather than written into the repo's config, so this never depends on or
# mutates ambient git setup.
CODE_FORGE_NAME = "code_forge"
CODE_FORGE_EMAIL = "forge@localhost"
CODE_FORGE_CO_AUTHOR = f"Co-authored-by: {CODE_FORGE_NAME} <{CODE_FORGE_EMAIL}>"


def commit_all(repo_path: str, message: str, *, author: tuple[str, str] | None = None) -> str | None:
    """Stage and commit all changes. Returns commit SHA or None if nothing to commit.

    `author` is the (name, email) the commit is credited to — a person's
    identity, so GitHub attributes the work to them. code_forge is then
    named as a co-author in the message trailer. With no `author`, the
    commit is authored by code_forge itself and carries no trailer, since
    that would only repeat the author."""
    status = _run_git(["status", "--porcelain"], cwd=repo_path)
    if status.returncode != 0:
        raise GitMergeError(status.stderr.strip() or "Could not read git status", code="status_failed", stderr=status.stderr)
    if not status.stdout.strip():
        head = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
        return head.stdout.strip() if head.returncode == 0 else None

    add = _run_git(["add", "-A"], cwd=repo_path)
    if add.returncode != 0:
        raise GitMergeError(add.stderr.strip() or "git add failed", code="commit_failed", stderr=add.stderr)

    name, email = author or (CODE_FORGE_NAME, CODE_FORGE_EMAIL)
    if author:
        message = f"{message}\n\n{CODE_FORGE_CO_AUTHOR}"
    commit = _run_git(["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message], cwd=repo_path)
    if commit.returncode != 0:
        raise GitMergeError(commit.stderr.strip() or "git commit failed", code="commit_failed", stderr=commit.stderr)

    head = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    return head.stdout.strip() if head.returncode == 0 else None


def list_unmerged_files(repo_path: str) -> list[str]:
    proc = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd=repo_path)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def merge_branch(repo_path: str, source_branch: str) -> None:
    proc = _run_git(["merge", source_branch, "--no-edit"], cwd=repo_path)
    if proc.returncode == 0:
        return

    conflict_files = list_unmerged_files(repo_path)
    abort = _run_git(["merge", "--abort"], cwd=repo_path)
    stderr = proc.stderr or proc.stdout or ""
    if abort.returncode != 0:
        stderr = f"{stderr}\n{abort.stderr}".strip()

    raise GitMergeError(f"Merge conflict merging {source_branch}", code="merge_conflict", conflict_files=conflict_files, stderr=stderr)


def list_changed_files(worktree_path: str) -> list[str]:
    """Return tracked diffs, staged changes, and untracked files."""
    names: set[str] = set()

    for args in (
        ["git", "-C", worktree_path, "diff", "--name-only", "HEAD"],
        ["git", "-C", worktree_path, "diff", "--name-only", "--cached"],
    ):
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            names.update(line.strip() for line in proc.stdout.splitlines() if line.strip())

    untracked = subprocess.run(
        ["git", "-C", worktree_path, "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=False,
    )
    if untracked.returncode == 0:
        names.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())

    return sorted(names)


def stage_files(worktree_path: str, paths: list[str]) -> None:
    if not paths:
        return
    subprocess.run(["git", "-C", worktree_path, "add", "--", *paths], capture_output=True, text=True, check=False)


def read_file_at_head(worktree_path: str, relative_path: str) -> str | None:
    """Read a tracked file as it existed at HEAD (before uncommitted edits)."""
    proc = subprocess.run(["git", "-C", worktree_path, "show", f"HEAD:{relative_path}"], capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def get_file_diff(worktree_path: str, relative_path: str) -> str:
    proc = subprocess.run(["git", "-C", worktree_path, "diff", "HEAD", "--", relative_path], capture_output=True, text=True, check=False)
    if proc.stdout.strip():
        return proc.stdout

    cached = subprocess.run(["git", "-C", worktree_path, "diff", "--cached", "--", relative_path], capture_output=True, text=True, check=False)
    if cached.stdout.strip():
        return cached.stdout

    abs_path = os.path.join(worktree_path, relative_path)
    if os.path.isfile(abs_path):
        return "[new or modified file]"
    return proc.stdout


def get_all_diffs(worktree_path: str) -> list[dict]:
    return [{"path": rel, "diff": get_file_diff(worktree_path, rel)} for rel in list_changed_files(worktree_path)]


def branch_exists(repo_path: str, branch: str) -> bool:
    proc = _run_git(["rev-parse", "-q", "--verify", f"refs/heads/{branch}"], cwd=repo_path)
    return proc.returncode == 0


def resolve_default_branch(repo_path: str) -> str:
    """Best-effort resolution of the repo's integration branch. Not every repo
    uses ``main``, so prefer the remote's advertised HEAD, then fall back
    through common names, then the current checkout."""
    head = _run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo_path)
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    for candidate in ("development", "develop", "main", "master"):
        if branch_exists(repo_path, candidate):
            return candidate
    try:
        return get_checkout_branch(repo_path)
    except GitMergeError:
        return "HEAD"


def list_branches_matching(repo_path: str, tokens: list[str]) -> list[str]:
    """Return local + remote branch short-names whose name contains any of
    *tokens* (case-insensitive), excluding agent/* branches."""
    wanted = [t.lower() for t in tokens if t]
    if not wanted:
        return []
    proc = _run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/", "refs/remotes/"], cwd=repo_path)
    if proc.returncode != 0:
        return []
    seen: set[str] = set()
    matches: list[str] = []
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name or name.endswith("/HEAD"):
            continue
        short = name[len("origin/"):] if name.startswith("origin/") else name
        if short.startswith("agent/"):
            continue
        low = short.lower()
        if short in seen or not any(tok in low for tok in wanted):
            continue
        seen.add(short)
        matches.append(short)
    return matches


def ref_exists(repo_path: str, ref: str) -> bool:
    """Whether *ref* currently resolves in *repo_path*."""
    proc = _run_git(["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd=repo_path)
    return proc.returncode == 0


def _resolve_ref(repo_path: str, name: str) -> str:
    if branch_exists(repo_path, name):
        return name
    if ref_exists(repo_path, f"refs/remotes/origin/{name}"):
        return f"origin/{name}"
    return name


def get_branch_diff_summary(repo_path: str, branch: str, *, base: str | None = None, max_chars: int = 8000) -> dict:
    """Diff `branch` against `base` (default: the repo's resolved default
    branch), using triple-dot diff so only the branch's own commits show."""
    base_ref = base or resolve_default_branch(repo_path)
    branch_ref = _resolve_ref(repo_path, branch)
    files_proc = _run_git(["diff", "--name-only", f"{base_ref}...{branch_ref}"], cwd=repo_path)
    changed_files = [line.strip() for line in files_proc.stdout.splitlines() if line.strip()]
    diff_proc = _run_git(["diff", f"{base_ref}...{branch_ref}"], cwd=repo_path)
    diff_text = diff_proc.stdout
    truncated = len(diff_text) > max_chars
    return {
        "base": base_ref,
        "branch": branch_ref,
        "changed_files": changed_files,
        "diff": diff_text[:max_chars],
        "diff_truncated": truncated,
    }
