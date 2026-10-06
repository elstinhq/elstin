import os
import shutil
from typing import Any

from elstin.tools.path_guard import PathGuardError, resolve_read_path, resolve_write_path


def _planned_relative_paths(plan: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("files_to_create", "files_to_modify", "context_files"):
        value = plan.get(key) or []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    rel = item.get("path")
                else:
                    rel = str(item)
                if rel:
                    paths.append(rel.lstrip("/"))
        elif isinstance(value, str) and value:
            paths.append(value.lstrip("/"))
    return paths


def sync_file_from_project(project_path: str, worktree_path: str, relative_path: str) -> bool:
    """Copy a file from the live project into the worktree when the worktree is missing it."""
    rel = relative_path.lstrip("/")
    worktree_file = os.path.join(worktree_path, rel)
    if os.path.isfile(worktree_file):
        return False

    try:
        source = resolve_read_path(project_path, rel)
        target = resolve_write_path(worktree_path, rel)
    except PathGuardError:
        return False

    if not os.path.isfile(source):
        return False

    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)
    return True


def sync_planned_files_from_project(
    project_path: str,
    worktree_path: str,
    plan: dict[str, Any],
) -> list[str]:
    synced: list[str] = []
    for rel in _planned_relative_paths(plan):
        if sync_file_from_project(project_path, worktree_path, rel):
            synced.append(rel)
    return synced
