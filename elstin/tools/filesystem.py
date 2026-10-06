import os
import shutil
import subprocess

from elstin.tools.path_guard import resolve_write_path


def write_file(worktree_path: str, relative_path: str, content: str) -> None:
    abs_path = resolve_write_path(worktree_path, relative_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as handle:
        handle.write(content)


def apply_unified_diff(worktree_path: str, relative_path: str, new_content: str) -> None:
    write_file(worktree_path, relative_path, new_content)


# --- Baseline snapshots -----------------------------------------------------
# In-place runs edit the user's real checkout, which may carry uncommitted work.
# Reverting to HEAD (git checkout HEAD) would discard that work, so instead we
# snapshot each file's pre-edit state the first time the agent touches it and
# restore from that snapshot. This preserves uncommitted, untracked, and
# gitignored content (e.g. generated *.config.dart) through rollbacks/retries.

_ABSENT_SUFFIX = ".__absent__"


def _snapshot_path(baseline_dir: str, relative_path: str) -> str:
    return os.path.join(baseline_dir, relative_path.lstrip("/"))


def snapshot_baseline(baseline_dir: str, worktree_path: str, relative_path: str) -> None:
    """Record a file's pre-edit state the first time the agent touches it.

    Captures the file exactly as it is on disk now (including uncommitted and
    gitignored content) so a later rollback restores *this* state rather than
    HEAD. Idempotent: only the first call for a path writes a snapshot. A file
    that does not exist yet gets an 'absent' marker so rollback can delete it.
    """
    if not baseline_dir:
        return
    snap = _snapshot_path(baseline_dir, relative_path)
    marker = snap + _ABSENT_SUFFIX
    if os.path.exists(snap) or os.path.exists(marker):
        return  # already snapshotted — keep the original pre-run state
    src = resolve_write_path(worktree_path, relative_path)
    os.makedirs(os.path.dirname(snap), exist_ok=True)
    if os.path.isfile(src):
        shutil.copy2(src, snap)
    else:
        with open(marker, "w", encoding="utf-8"):
            pass


def _restore_one(baseline_dir: str, worktree_path: str, relative_path: str) -> bool:
    """Restore one file from its baseline snapshot. Returns True if a baseline
    existed (a snapshot was applied, or an agent-created file was deleted)."""
    snap = _snapshot_path(baseline_dir, relative_path)
    marker = snap + _ABSENT_SUFFIX
    dest = resolve_write_path(worktree_path, relative_path)
    if os.path.isfile(snap):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(snap, dest)
        return True
    if os.path.exists(marker):
        if os.path.isfile(dest):
            os.remove(dest)
        return True
    return False


def roll_back_file(
    worktree_path: str, relative_path: str, baseline_dir: str | None = None
) -> None:
    """Undo the agent's edits to a file.

    With a baseline snapshot, restore the file to the state the run started from
    (preserving pre-existing uncommitted work). Without one, fall back to the
    legacy behavior of reverting to the committed HEAD.
    """
    if baseline_dir and _restore_one(baseline_dir, worktree_path, relative_path):
        return

    abs_path = resolve_write_path(worktree_path, relative_path)
    if not os.path.exists(abs_path):
        return
    subprocess.run(
        ["git", "checkout", "HEAD", "--", relative_path],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )


def restore_all_baselines(baseline_dir: str, worktree_path: str) -> list[str]:
    """Restore every snapshotted file to its baseline (used by retry revert).

    Undoes only the agent's edits and leaves files it never touched — i.e. the
    user's other uncommitted work — alone. Returns the restored relative paths.
    """
    if not baseline_dir or not os.path.isdir(baseline_dir):
        return []
    restored: list[str] = []
    for root, _dirs, files in os.walk(baseline_dir):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), baseline_dir)
            if rel.endswith(_ABSENT_SUFFIX):
                rel = rel[: -len(_ABSENT_SUFFIX)]
            if _restore_one(baseline_dir, worktree_path, rel):
                restored.append(rel)
    return restored
