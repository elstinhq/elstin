"""Clones a project's git remote into storage the API process controls
(forge/db/models/project.py's ProjectRepo). Replaces the old "attach an
existing local folder" flow: that only worked when the API process could
already see the path (true for a local `uvicorn --reload` dev run, false
inside `docker compose up`, which has no view of the host filesystem
unless it's explicitly bind-mounted). Cloning works identically in both.
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
