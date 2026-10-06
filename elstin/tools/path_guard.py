"""Path-traversal guard. Every file-access tool in elstin/tools/ routes
through this to prevent traversal outside the project/worktree root."""
import os


class PathGuardError(ValueError):
    pass


def resolve_read_path(project_path: str, relative_path: str) -> str:
    root = os.path.realpath(project_path)
    candidate = os.path.realpath(os.path.join(root, relative_path.lstrip("/")))
    if not candidate.startswith(root + os.sep) and candidate != root:
        raise PathGuardError(f"Path traversal blocked: {relative_path}")
    return candidate


def resolve_write_path(worktree_path: str, relative_path: str) -> str:
    root = os.path.realpath(worktree_path)
    candidate = os.path.realpath(os.path.join(root, relative_path.lstrip("/")))
    if not candidate.startswith(root + os.sep) and candidate != root:
        raise PathGuardError(f"Path traversal blocked: {relative_path}")
    return candidate


def to_relative(project_path: str, absolute_path: str) -> str:
    root = os.path.realpath(project_path)
    path = os.path.realpath(absolute_path)
    if not path.startswith(root + os.sep) and path != root:
        raise PathGuardError(f"Path outside project: {absolute_path}")
    return os.path.relpath(path, root)
