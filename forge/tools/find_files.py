"""Find files by name or path glob across a workspace's read roots.

The one implementation behind both roles' `find_files_tool`. The planner had
it; dev did not, and a dev agent with no way to ask "what is in this folder"
reaches for `ls` or `find` through run_command_tool, which safe_shell.py
(rightly) refuses — so each attempt was a turn that changed nothing, and
enough of those trip the idle-turn budget before dev has written a file.
Read-only by construction: it walks directories and returns paths.
"""
from __future__ import annotations

import fnmatch
import os

from forge.tools.grep import DEFAULT_EXCLUDES
from forge.tools.workspace import Workspace

MAX_FIND_RESULTS = 200

FIND_FILES_DESCRIPTION = (
    "Find files by name/path glob pattern (e.g. '*.py', '**/test_*.py', 'lib/**/*.dart'). "
    "Use this to discover what exists before reading or searching it. To list a folder, "
    "match everything under it: 'src/main/resources/db/migration/*'."
)


def find_files(workspace: Workspace, glob_pattern: str, max_results: int = MAX_FIND_RESULTS) -> str:
    """Paths (workspace display form, so a non-default repo carries its
    @label prefix) whose name or project-relative path matches the glob.
    Hidden folders and the usual dependency/build folders are skipped."""
    limit = max(1, min(max_results or MAX_FIND_RESULTS, MAX_FIND_RESULTS))
    matches: list[str] = []
    for label, repo_root in workspace.read_roots():
        root = os.path.realpath(repo_root)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            # Sorted so the same call returns the same paths in the same
            # order, and a cap cuts at a predictable point.
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in DEFAULT_EXCLUDES)
            for name in sorted(filenames):
                rel_path = name if rel_dir == "." else f"{rel_dir}/{name}"
                if fnmatch.fnmatch(rel_path, glob_pattern) or fnmatch.fnmatch(name, glob_pattern):
                    found.append(workspace.display_path(label, rel_path))
                    if len(found) + len(matches) >= limit:
                        break
            if len(found) + len(matches) >= limit:
                break
        # Sorted per root so the primary repo's files stay grouped ahead of
        # each reference repo's.
        matches.extend(sorted(found))
        if len(matches) >= limit:
            break
    if not matches:
        return "No files matched."
    suffix = "\n... [truncated — narrow your pattern]" if len(matches) >= limit else ""
    return "\n".join(matches) + suffix
