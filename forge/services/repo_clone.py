"""The two ways a project gets a git repo (forge/db/models/project.py's
ProjectRepo):

- **Clone** (`clone_repository`): a remote URL, cloned into storage the API
  process controls. Works the same whether forge runs on the host or inside
  `docker compose up`.
- **Attach** (`attach_local_repository`): a repo that already exists on disk,
  used in place. Only works for a path the forge process can see — any path
  when forge runs directly on the host (`uvicorn`), and inside Docker only a
  folder bind-mounted into the app container (see docker-compose.yml). The
  errors say so, because "no such folder" for a path that plainly exists on
  the host is otherwise baffling.

forge never changes an attached repo's checked-out files: it fetches,
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

from forge.config.store import get_config

logger = logging.getLogger("forge.repo_clone")

_CLONE_TIMEOUT_SECONDS = 300
_ERROR_MESSAGE_MAX_CHARS = 2000


class RepoCloneError(RuntimeError):
    pass


class RepoAttachError(RuntimeError):
    pass


def attach_local_repository(path: str, *, forbidden_dirs: list[str] | None = None) -> str:
    """Validates `path` as a repo forge can work from, and returns it as an
    absolute, symlink-resolved path. Refuses — with a message naming what to
    do instead — anything that is not the top folder of a normal git
    repository with at least one commit, and anything inside `forbidden_dirs`
    (forge's own worktrees: attaching one of those would have forge branch
    from its own scratch space)."""
    raw = (path or "").strip()
    if not raw:
        raise RepoAttachError("Folder path is required")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise RepoAttachError(f"Give the full path to the folder (e.g. /Users/you/code/app), not {raw!r}")
    folder = os.path.realpath(expanded)
    if not os.path.isdir(folder):
        raise RepoAttachError(
            f"No folder at {folder} on the machine forge runs on. Running in Docker? Only folders mounted "
            "into the app container are visible — mount it (see docker-compose.yml) and attach the path "
            "inside the container, or clone the repo by URL instead."
        )
    for managed in forbidden_dirs or []:
        managed_real = os.path.realpath(managed)
        if folder == managed_real or folder.startswith(managed_real + os.sep):
            raise RepoAttachError(f"{folder} is inside forge's own working folder ({managed_real}) — attach the original repo")

    top = _git_output(folder, "rev-parse", "--show-toplevel")
    if top is None:
        raise RepoAttachError(f"{folder} is not a git repository — run `git init` there, or attach its repository folder")
    if os.path.realpath(top) != folder:
        raise RepoAttachError(f"{folder} is inside the git repository at {top} — attach that top folder instead")
    if _git_output(folder, "rev-parse", "--verify", "HEAD") is None:
        raise RepoAttachError(f"{folder} has no commits yet — forge branches every story from an existing commit")
    logger.info("attached local repo %s", folder)
    return folder


def _git_output(cwd: str, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def clone_repository(url: str, *, dest_base_dir: str, timeout: int = _CLONE_TIMEOUT_SECONDS) -> str:
    """Clones `url` into a fresh, uniquely-named directory under
    `dest_base_dir`. Returns the absolute path of the new checkout.

    A full clone, not `--single-branch` or `--depth`: downstream code
    (forge/tools/worktree.py's create_worktree, resuming a story from an
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
    return rewritten, {"GIT_ASKPASS": askpass_path, "FORGE_CLONE_GIT_TOKEN": token}, askpass_path


def _write_askpass_script() -> str:
    fd, path = tempfile.mkstemp(prefix="forge-git-askpass-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write('#!/bin/sh\necho "$FORGE_CLONE_GIT_TOKEN"\n')
    os.chmod(path, 0o700)
    return path
