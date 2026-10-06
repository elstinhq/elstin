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

from elstin.tools.grep import DEFAULT_EXCLUDES
from elstin.tools.path_guard import PathGuardError
from elstin.tools.workspace import REFERENCE_SIGIL, Workspace

MAX_FIND_RESULTS = 200

FIND_FILES_DESCRIPTION = (
    "Find files by name/path glob pattern (e.g. '*.py', '**/test_*.py', 'lib/**/*.dart'). "
    "Use this to discover what exists before reading or searching it. To list a folder, "
    "match everything under it: 'src/main/resources/db/migration/*'."
)


def _matches(rel_path: str, name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(name, p) for p in patterns)


def find_files(workspace: Workspace, glob_pattern: str, max_results: int = MAX_FIND_RESULTS) -> str:
    """Paths (workspace display form, so a non-default repo carries its
    @label prefix) whose name or project-relative path matches the glob.
    Hidden folders and the usual dependency/build folders are skipped.

    A glob may start with `@repo/` — the form the results themselves come back
    in — to search that repo alone. Without it a model that copies a result's
    path into its next glob gets "No files matched." for files that exist, and
    reasonably concludes they do not.

    `**/` also matches no directory at all, as it does in a shell glob: fnmatch
    alone reads `src/Customers/**/*` as "at least one folder below Customers" and
    skips the files directly in it."""
    limit = max(1, min(max_results or MAX_FIND_RESULTS, MAX_FIND_RESULTS))
    matches: list[str] = []
    roots = workspace.read_roots()
    if glob_pattern.strip().startswith(REFERENCE_SIGIL):
        try:
            label, glob_pattern = workspace.split(glob_pattern)
        except PathGuardError as exc:
            return str(exc)
        glob_pattern = glob_pattern or "*"
        roots = [(label, workspace.roots[label])]
    # fnmatch's `*` already crosses `/`; what it lacks is `**/` standing for no folder.
    patterns = (glob_pattern, glob_pattern.replace("**/", "")) if "**/" in glob_pattern else (glob_pattern,)
    for label, repo_root in roots:
        root = os.path.realpath(repo_root)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            # Sorted so the same call returns the same paths in the same
            # order, and a cap cuts at a predictable point.
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in DEFAULT_EXCLUDES)
            for name in sorted(filenames):
                rel_path = name if rel_dir == "." else f"{rel_dir}/{name}"
                if _matches(rel_path, name, patterns):
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
