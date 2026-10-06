"""The two ways a project gets a git repo (elstin/db/models/project.py's
ProjectRepo):

- **Clone** (`clone_repository`): a remote URL, cloned into storage the API
  process controls. Works the same whether elstin runs on the host or inside
  `docker compose up`.
- **Attach** (`attach_local_repository`): a repo that already exists on disk,
  used in place. Only works for a path the elstin process can see — any path
  when elstin runs directly on the host (`uvicorn`), and inside Docker only a
  folder bind-mounted into the app container (see docker-compose.yml). The
  errors say so, because "no such folder" for a path that plainly exists on
  the host is otherwise baffling. A folder that is not a repo but holds
  several (one checkout per service) is attached repo by repo:
  `scan_folder` lists them and a person picks which.

elstin never changes an attached repo's checked-out files: it fetches,
creates its story branches and worktrees from the repo's refs, and keeps the
worktrees under its own worktrees folder. The branches do appear in the
repo's `git branch` list until a ticket is removed.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

from elstin.config.store import get_config
from elstin.tools.reference_folder import plan_reference

logger = logging.getLogger("elstin.repo_clone")

_CLONE_TIMEOUT_SECONDS = 300
_ERROR_MESSAGE_MAX_CHARS = 2000


class RepoCloneError(RuntimeError):
    pass


class RepoAttachError(RuntimeError):
    pass


def attach_local_repository(path: str, *, forbidden_dirs: list[str] | None = None) -> str:
    """Validates `path` as a repo elstin can work from, and returns it as an
    absolute, symlink-resolved path. Refuses — with a message naming what to
    do instead — anything that is not the top folder of a normal git
    repository with at least one commit, and anything inside `forbidden_dirs`
    (elstin's own worktrees: attaching one of those would have elstin branch
    from its own scratch space).

    A folder that is not a repo but HOLDS repos (a workspace folder with one
    checkout per service) is refused too, naming them: each is attached on
    its own — see scan_folder, which is how the UI offers the choice."""
    folder = _resolve_folder(path, forbidden_dirs)
    top = _git_output(folder, "rev-parse", "--show-toplevel")
    if top is None:
        inside = [repo["name"] for repo in _child_repos(folder)]
        if inside:
            shown = ", ".join(inside[:8]) + (f" and {len(inside) - 8} more" if len(inside) > 8 else "")
            raise RepoAttachError(
                f"{folder} is not a git repository, but it holds {len(inside)}: {shown} — attach those instead"
            )
        raise RepoAttachError(f"{folder} is not a git repository — run `git init` there, or attach its repository folder")
    if os.path.realpath(top) != folder:
        raise RepoAttachError(f"{folder} is inside the git repository at {top} — attach that top folder instead")
    if _git_output(folder, "rev-parse", "--verify", "HEAD") is None:
        raise RepoAttachError(f"{folder} has no commits yet — elstin branches every story from an existing commit")
    logger.info("attached local repo %s", folder)
    return folder


def attach_reference_folder(path: str, *, forbidden_dirs: list[str] | None = None) -> str:
    """Validates `path` as a reference folder — a plain folder read for
    context, never written (elstin/tools/reference_folder.py) — and returns
    it resolved. A git repo is refused: attach it as a repo and mark it
    read-only, so runs read its committed state through git like any repo."""
    folder = _resolve_folder(path, forbidden_dirs)
    if _git_output(folder, "rev-parse", "--show-toplevel") is not None:
        raise RepoAttachError(f"{folder} is a git repository — attach it as a folder and mark it read-only instead")
    logger.info("attached reference folder %s", folder)
    return folder


def scan_folder(path: str, *, forbidden_dirs: list[str] | None = None) -> dict:
    """What attaching `path` would mean. Read-only.

    Every git operation elstin makes — a branch and worktree per story, local
    merges, pushing the epic branch — happens inside one repository, so a
    folder that is not one cannot be attached as it is. A folder that HOLDS
    repos can be, one repo at a time: this lists them so a person picks which.

    {"path": resolved, "is_repo": True, "repos": []} for a repo's top folder;
    {"path": resolved, "is_repo": False, "repos": [{"path", "name", "problem"}]}
    otherwise — the git repos directly inside it (hidden folders skipped),
    `problem` saying why one cannot be attached (no commits yet, say), else
    None. Raises RepoAttachError for a path that is not usable at all, or one
    inside a repository (attach its top folder).

    For a folder that is not a repo, `reference` says what else in it could
    be read as a reference folder — its CLAUDE.md, docs and the like — and
    what would be left out (data files, the repos themselves): see
    elstin/tools/reference_folder.py."""
    folder = _resolve_folder(path, forbidden_dirs)
    top = _git_output(folder, "rev-parse", "--show-toplevel")
    if top is not None:
        if os.path.realpath(top) != folder:
            raise RepoAttachError(f"{folder} is inside the git repository at {top} — attach that top folder instead")
        return {"path": folder, "is_repo": True, "repos": []}
    repos = []
    for repo in _child_repos(folder):
        try:
            attach_local_repository(repo["path"], forbidden_dirs=forbidden_dirs)
            repo["problem"] = None
        except RepoAttachError as exc:
            # Shown under the repo's name, so without its full path again.
            repo["problem"] = str(exc).removeprefix(repo["path"]).strip()
        repos.append(repo)
    return {"path": folder, "is_repo": False, "repos": repos, "reference": plan_reference(folder).summary()}


# Enough to scan a big workspace folder by eye; a listing past this is cut
# and says so, rather than shipping thousands of rows to a picker.
MAX_BROWSE_ENTRIES = 1000


def browse_folder(path: str | None) -> dict:
    """The subfolders of `path` (home when empty), for the UI's folder
    picker. Read-only, names only — no file is opened.

    A browser cannot hand a page the real path of a folder picked in the
    operating system's dialog, and the path that matters is the one the
    elstin process sees (inside Docker, a mount point, not the host's path).
    So the picker browses through elstin. Hidden folders are skipped; a git
    repository is marked, since that is usually what is being looked for."""
    raw = (path or "").strip() or "~"
    folder = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isabs(os.path.expanduser(raw)):
        raise RepoAttachError(f"Give the full path to the folder (e.g. /Users/you/code), not {raw!r}")
    if not os.path.isdir(folder):
        raise RepoAttachError(f"No folder at {folder} on the machine elstin runs on")
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError as exc:
        raise RepoAttachError(f"elstin cannot read {folder}: {exc.strerror or exc}") from exc
    entries: list[dict] = []
    truncated = False
    for name in names:
        child = os.path.join(folder, name)
        if name.startswith(".") or not os.path.isdir(child):
            continue
        if len(entries) >= MAX_BROWSE_ENTRIES:
            truncated = True
            break
        entries.append({"name": name, "path": child, "is_repo": os.path.exists(os.path.join(child, ".git"))})
    parent = os.path.dirname(folder)
    return {
        "path": folder,
        "parent": parent if parent != folder else None,
        "home": os.path.expanduser("~"),
        "is_repo": os.path.exists(os.path.join(folder, ".git")),
        "entries": entries,
        "truncated": truncated,
    }


def _resolve_folder(path: str, forbidden_dirs: list[str] | None) -> str:
    raw = (path or "").strip()
    if not raw:
        raise RepoAttachError("Folder path is required")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise RepoAttachError(f"Give the full path to the folder (e.g. /Users/you/code/app), not {raw!r}")
    folder = os.path.realpath(expanded)
    if not os.path.isdir(folder):
        raise RepoAttachError(
            f"No folder at {folder} on the machine elstin runs on. Running in Docker? Only folders mounted "
            "into the app container are visible — mount it (see docker-compose.yml) and attach the path "
            "inside the container, or clone the repo by URL instead."
        )
    for managed in forbidden_dirs or []:
        managed_real = os.path.realpath(managed)
        if folder == managed_real or folder.startswith(managed_real + os.sep):
            raise RepoAttachError(f"{folder} is inside elstin's own working folder ({managed_real}) — attach the original repo")
    return folder


def _child_repos(folder: str) -> list[dict]:
    """The git repositories directly inside `folder`: a folder holding a
    `.git` (a directory, or a file for a linked worktree). One level only —
    a workspace folder is a list of checkouts, and walking deeper would find
    vendored and nested repos nobody meant."""
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    found = []
    for name in names:
        child = os.path.join(folder, name)
        if name.startswith(".") or not os.path.isdir(child) or not os.path.exists(os.path.join(child, ".git")):
            continue
        found.append({"path": os.path.realpath(child), "name": name})
    return found


def _git_output(cwd: str, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def clone_repository(url: str, *, dest_base_dir: str, timeout: int = _CLONE_TIMEOUT_SECONDS) -> str:
    """Clones `url` into a fresh, uniquely-named directory under
    `dest_base_dir`. Returns the absolute path of the new checkout.

    A full clone, not `--single-branch` or `--depth`: downstream code
    (elstin/tools/worktree.py's create_worktree, resuming a story from an
    already-pushed branch) inspects `origin/<branch>` refs for branches
    other than the default, which a shallow clone would hide.
    """
    url = (url or "").strip()
    if not url:
        raise RepoCloneError("Repository URL is required")

    try:
        os.makedirs(dest_base_dir, exist_ok=True)
    except OSError as exc:
        raise RepoCloneError(
            f"Could not create {dest_base_dir!r}: {exc.strerror or exc}. "
            "Check the 'Cloned project repos base directory' setting — it must be a path "
            "this process can write to (e.g. /data/cloned-repos inside Docker, or a real "
            "local path when running outside Docker)."
        ) from exc
    dest = _unique_dest_path(dest_base_dir, _derive_repo_name(url))

    clone_url, extra_env, askpass_path = _resolve_auth(url)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **extra_env}

    try:
        proc = subprocess.run(
            ["git", "clone", clone_url, dest],
            capture_output=True, text=True, check=False, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise RepoCloneError(f"git clone timed out after {timeout}s") from exc
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        detail = (proc.stderr or proc.stdout or "git clone failed").strip()[-_ERROR_MESSAGE_MAX_CHARS:]
        raise RepoCloneError(detail)

    if not os.path.isdir(os.path.join(dest, ".git")):
        shutil.rmtree(dest, ignore_errors=True)
        raise RepoCloneError("git clone reported success but no .git directory was created")

    logger.info("cloned %s -> %s", url, dest)
    return os.path.abspath(dest)


def _derive_repo_name(url: str) -> str:
    trimmed = url.strip().rstrip("/")
    if "://" not in trimmed and "@" in trimmed and ":" in trimmed:
        # scp-like syntax, e.g. git@github.com:owner/repo.git
        tail = trimmed.rsplit(":", 1)[-1]
    else:
        tail = urlparse(trimmed).path
    name = tail.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-._")
    return name or "repo"


def _unique_dest_path(dest_base_dir: str, name: str) -> str:
    candidate = os.path.join(dest_base_dir, name)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(dest_base_dir, f"{name}-{n}")
        n += 1
    return candidate


def _resolve_auth(url: str) -> tuple[str, dict[str, str], str | None]:
    """For a bare (no embedded credentials) https://github.com/... URL with
    a GitHub token configured: rewrites the URL to carry a fixed
    `x-access-token` username and returns extra subprocess env that
    supplies the real token via GIT_ASKPASS at run time. The token itself
    never appears in argv, where it would be visible to any local user via
    `ps` for the life of the clone. Any other URL (a different host, or one
    that already embeds its own credentials) passes through unchanged —
    auth for those is the caller's responsibility."""
    parsed = urlparse(url)
    if parsed.hostname != "github.com" or parsed.scheme != "https" or parsed.username:
        return url, {}, None
    token = get_config("github_token")
    if not token:
        return url, {}, None

    netloc = "x-access-token@" + parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    rewritten = parsed._replace(netloc=netloc).geturl()
    askpass_path = _write_askpass_script()
    return rewritten, {"GIT_ASKPASS": askpass_path, "ELSTIN_CLONE_GIT_TOKEN": token}, askpass_path


def _write_askpass_script() -> str:
    fd, path = tempfile.mkstemp(prefix="elstin-git-askpass-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write('#!/bin/sh\necho "$ELSTIN_CLONE_GIT_TOKEN"\n')
    os.chmod(path, 0o700)
    return path
