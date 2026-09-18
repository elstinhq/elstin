"""A cheap, pre-computed map of a repo — every source file's path and its
top-level declarations — that the daemon folds into the planner's brief
(forge/daemon/main.py's `_build_story_brief`).

Why it exists: without it, a planner run's first several turns are pure
directory exploration (find_files, grep, read the wrong file, read the
right one), and each of those turns re-sends the whole prompt. A weaker
model is worse at this than a frontier one and burns proportionally more
turns on it. A few kilobytes of "here is what exists and what it
declares" up front lets the model go straight to the files that matter —
it is the poor man's version of the RAG pre-seeding AGENTS.md lists under
"Not built yet", built from a filesystem walk and one regex instead of an
embedding store.

Deliberately language-neutral: which files count as source comes from
the registered LanguageProfiles (`is_source`/`is_test`) when the repo
matches one, with a generic extension fallback otherwise, and the
declaration regex only recognizes the declaration keywords most
languages share (`class`, `interface`, `enum`, `def`, `function`, …) at
column 0 — so it lists top-level declarations without knowing any one
language's grammar. Nothing here should ever branch on a language name.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict

from forge.tools import language_hooks
from forge.tools.grep import DEFAULT_EXCLUDES
from forge.tools.workspace import REFERENCE_SIGIL

# Output cap. Roughly 3k tokens — large enough to cover a mid-sized app's
# source tree, small enough that it never crowds out the ticket text.
DEFAULT_MAX_CHARS = 12_000
# Walk caps: beyond this a repo is big enough that a skeleton would be
# truncated into uselessness anyway, and the walk itself starts to cost.
_MAX_FILES = 1_500
_MAX_FILE_BYTES = 256_000

# Source extensions to fall back on when no LanguageProfile matches the
# repo (a language forge has no toolchain profile for yet). Extensions,
# not behavior — the skeleton reads every file the same way.
_GENERIC_SOURCE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", ".svelte", ".dart", ".java", ".kt", ".kts",
    ".go", ".rs", ".rb", ".cs", ".swift", ".scala", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".ex", ".exs",
})

# Column-0 declarations only, so nested/member declarations (indented in
# every language forge meets) are skipped and the list stays top-level.
_DECLARATION_RE = re.compile(
    r"^(?:export\s+(?:default\s+)?)?"
    r"(?:(?:public|private|protected|internal|abstract|final|sealed|static|async|open|data|partial|declare)\s+)*"
    r"(?:class|interface|enum|mixin|extension|record|struct|trait|protocol|def|function|func|fn|type|typedef|object|module|impl)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
# `export const useAuth = ...` / `export let ...` — the module-level
# binding shape JS/TS libraries export from.
_EXPORT_BINDING_RE = re.compile(r"^export\s+(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def _is_source_file(rel_path: str, profiles: list[language_hooks.LanguageProfile]) -> bool:
    if profiles:
        return any(p.is_source(rel_path) or p.is_test(rel_path) for p in profiles)
    return os.path.splitext(rel_path)[1] in _GENERIC_SOURCE_EXTENSIONS


def extract_declarations(source: str) -> list[str]:
    """Top-level declaration names in `source`, in order, de-duplicated."""
    names: list[str] = []
    seen: set[str] = set()
    for match in _DECLARATION_RE.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    for match in _EXPORT_BINDING_RE.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def scan_repo(project_path: str, *, max_files: int = _MAX_FILES) -> dict[str, list[str]]:
    """{relative_path: [declaration, ...]} for every source file, walked
    with the same exclusions grep uses (build output, deps, VCS)."""
    root = os.path.realpath(project_path)
    if not os.path.isdir(root):
        return {}
    profiles = language_hooks.detect_profiles(root)
    files: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in DEFAULT_EXCLUDES)
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(filenames):
            rel_path = name if rel_dir == "." else f"{rel_dir}/{name}".replace(os.sep, "/")
            if not _is_source_file(rel_path, profiles):
                continue
            abs_path = os.path.join(dirpath, name)
            try:
                if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
                    files[rel_path] = []
                    continue
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as handle:
                    files[rel_path] = extract_declarations(handle.read())
            except OSError:
                continue
            if len(files) >= max_files:
                return files
    return files


def render_skeleton(files: dict[str, list[str]], *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """One line per directory: `dir/: file [Decl, Decl], file, ...` —
    grouping by directory is what keeps a 400-file tree inside a few
    thousand tokens. Truncates on a line boundary with a count of what
    was left out, so the model knows the map is partial rather than
    concluding those files don't exist."""
    if not files:
        return ""
    by_dir: dict[str, list[str]] = defaultdict(list)
    for rel_path in sorted(files):
        directory, _, name = rel_path.rpartition("/")
        decls = files[rel_path]
        by_dir[directory].append(f"{name} [{', '.join(decls[:12])}{', …' if len(decls) > 12 else ''}]" if decls else name)

    lines: list[str] = []
    used = 0
    rendered_files = 0
    for directory in sorted(by_dir):
        line = f"{directory + '/' if directory else './'}: " + ", ".join(by_dir[directory])
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        rendered_files += len(by_dir[directory])

    omitted = len(files) - rendered_files
    if omitted > 0:
        lines.append(f"... ({omitted} more source file(s) omitted — use find_files_tool/grep_tool for the rest)")
    return "\n".join(lines)


def build_workspace_skeleton(
    primary_path: str,
    reference_roots: dict[str, str] | None = None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """A skeleton per repo in the workspace, primary first.

    The budget is split rather than granted per repo: a project with four
    repos would otherwise quadruple the brief. The primary repo gets the
    larger share because it is the one the story changes, and each
    reference repo's section is labelled with the `@label/` prefix its
    paths must carry, so the map doubles as documentation of how to
    address them."""
    references = dict(reference_roots or {})
    primary = build_codebase_skeleton(primary_path, max_chars=max_chars if not references else max_chars // 2)
    if not references:
        return primary

    # Whatever the primary did not use stays available to the references,
    # so a small primary repo does not waste half the budget.
    remaining = max(max_chars - len(primary), max_chars // 4)
    per_reference = max(remaining // len(references), 600)

    sections = [primary] if primary else []
    for label in sorted(references):
        body = build_codebase_skeleton(references[label], max_chars=per_reference)
        if not body:
            continue
        sections.append(
            f"### Reference repo {REFERENCE_SIGIL}{label} (read-only)\n"
            f"Address these files as {REFERENCE_SIGIL}{label}/<path>. You cannot modify this repo.\n"
            + body
        )
    return "\n\n".join(sections)


def build_codebase_skeleton(project_path: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The brief section: a header explaining what the map is for, then
    the rendered tree. Empty string when the repo has no recognizable
    source files, so the caller can append it unconditionally."""
    files = scan_repo(project_path)
    body = render_skeleton(files, max_chars=max_chars)
    if not body:
        return ""
    return (
        f"## Codebase skeleton ({len(files)} source files, pre-computed)\n"
        "Every source file with its top-level declarations, grouped by directory. Use it to go "
        "straight to the relevant files with read_file_tool/grep_tool instead of exploring the tree.\n"
        + body
    )
