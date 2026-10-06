"""A reference folder: a plain folder read for context, never written.

The motivating case is a workspace folder like `~/dev/mono`: one checkout per
service side by side, plus what sits around them — a CLAUDE.md about how the
repos relate ("API-Schemas is off limits"), `docs/` with a
knowledge base and plans, a shell script, and gigabytes of database dumps and
spreadsheets. The repos are attached one by one (elstin/services/repo_clone.py).
The rest is context the planner and dev should read — the dumps are not.

Runs never read the folder itself. They read a COPY of its readable part,
refreshed when a run starts (`sync_reference_copy`), which is what makes the
rules hold for every tool without teaching each one about them:

- the repos inside it are left out — each is read through its own attachment
  (a worktree, or the epic branch), never as a stale checkout under another
  name; and a repo that was not attached was left out on purpose;
- hidden entries, dependency/build folders, and data files — archives, dumps,
  spreadsheets, images, anything over `MAX_FILE_BYTES` — are left out;
- the copy stops at `MAX_TOTAL_BYTES`.

And nothing a run does can reach the original: it only ever sees the copy.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field

from elstin.tools.grep import DEFAULT_EXCLUDES

MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 25_000_000
# A copy younger than this is reused: parallel stories starting together
# share one, rather than each rebuilding it under the others' reads.
FRESH_SECONDS = 60

# Data, not context: archives and dumps, spreadsheets and exports, media,
# binaries. Text a model can use is everything else under the size cap.
DATA_EXTENSIONS = frozenset({
    ".gz", ".tgz", ".zip", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".sql", ".dump", ".bak", ".db", ".sqlite", ".sqlite3", ".parquet", ".avro",
    ".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".ods", ".numbers",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".key",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".ico", ".webp", ".heic", ".psd",
    ".mp4", ".mov", ".avi", ".mp3", ".wav",
    ".jar", ".war", ".class", ".exe", ".dll", ".so", ".dylib", ".bin", ".iso", ".dmg",
})

_MARKER = ".elstin-reference"


@dataclass
class ReferencePlan:
    """What a copy of the folder holds and leaves out."""
    files: list[tuple[str, int]] = field(default_factory=list)  # (relative path, size)
    repos: list[str] = field(default_factory=list)  # relative paths of the git repos left out
    skipped: dict[str, str] = field(default_factory=dict)  # relative path -> why
    total_bytes: int = 0

    def summary(self) -> dict:
        """For the UI: the top-level entries that will be readable, and those
        left out with the reason — per entry, not per file, since `db/`
        skipped whole is one line, not five hundred."""
        readable: dict[str, None] = {}
        for rel, _size in self.files:
            readable.setdefault(_top(rel), None)
        skipped: dict[str, str] = {}
        for rel, why in self.skipped.items():
            top = _top(rel)
            if top not in readable:
                skipped.setdefault(top, why)
        return {
            "readable": sorted(readable, key=str.lower),
            "skipped": [{"name": name, "reason": why} for name, why in sorted(skipped.items(), key=lambda i: i[0].lower())],
            "bytes": self.total_bytes,
        }


def plan_reference(source: str) -> ReferencePlan:
    """What `source` contributes to a reference copy. Read-only."""
    plan = ReferencePlan()
    source = os.path.realpath(source)
    for dirpath, dirnames, filenames in os.walk(source):
        rel_dir = os.path.relpath(dirpath, source)
        keep = []
        for name in sorted(dirnames, key=str.lower):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if name.startswith(".") or name in DEFAULT_EXCLUDES:
                continue
            if os.path.exists(os.path.join(dirpath, name, ".git")):
                plan.repos.append(rel)
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames, key=str.lower):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if os.path.splitext(name)[1].lower() in DATA_EXTENSIONS:
                plan.skipped[rel] = "data file"
                continue
            size = os.path.getsize(path)
            if size > MAX_FILE_BYTES:
                plan.skipped[rel] = f"over {MAX_FILE_BYTES // 1_000_000} MB"
                continue
            if plan.total_bytes + size > MAX_TOTAL_BYTES:
                plan.skipped[rel] = f"past the {MAX_TOTAL_BYTES // 1_000_000} MB total"
                continue
            if _looks_binary(path):
                plan.skipped[rel] = "binary file"
                continue
            plan.files.append((rel, size))
            plan.total_bytes += size
    return plan


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def sync_reference_copy(source: str, dest: str) -> str:
    """Refresh the copy of `source`'s readable part at `dest` and return
    `dest`. A copy made in the last FRESH_SECONDS is reused. Built beside
    `dest` and swapped in, so a reader never sees a half-written copy. A
    source that has gone leaves the last copy in place — reading slightly
    stale notes beats failing a run over them."""
    dest = os.path.realpath(dest)
    with _locks_guard:
        lock = _locks.setdefault(dest, threading.Lock())
    with lock:
        marker = os.path.join(dest, _MARKER)
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < FRESH_SECONDS:
            return dest
        if not os.path.isdir(source):
            os.makedirs(dest, exist_ok=True)
            return dest
        staging = f"{dest}.staging-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        for rel, _size in plan_reference(source).files:
            target = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(source, rel), target)
        with open(os.path.join(staging, _MARKER), "w") as handle:
            handle.write(f"copy of {os.path.realpath(source)}\n")
        old = f"{dest}.old-{os.getpid()}"
        if os.path.exists(dest):
            os.replace(dest, old)
        os.replace(staging, dest)
        shutil.rmtree(old, ignore_errors=True)
        return dest


def reference_copy_path(worktrees_base_dir: str, repo_id: int) -> str:
    """Where a reference folder's copy lives: elstin's own storage, beside
    the worktrees, keyed by the folder's id."""
    return os.path.join(worktrees_base_dir, "_reference", str(repo_id))


def remove_reference_copy(worktrees_base_dir: str, repo_id: int) -> bool:
    """Delete a reference folder's copy (the folder itself is never
    touched), and the `_reference` folder once nothing is left in it."""
    path = reference_copy_path(worktrees_base_dir, repo_id)
    existed = os.path.isdir(path)
    shutil.rmtree(path, ignore_errors=True)
    try:
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass
    return existed


def _top(rel: str) -> str:
    head, _, rest = rel.partition("/")
    return f"{head}/" if rest else head


def _looks_binary(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return b"\0" in handle.read(4096)
    except OSError:
        return True
