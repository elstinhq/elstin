"""Read-only tools for the planner (forge/agents/roles/planner_loop.py) —
forge's own equivalents of the Claude Agent SDK's built-in Read/Grep/Glob,
which the planner borrowed before this migration. No provider offers
built-in tools anymore (forge/llm/base.py's LlmProvider is a plain
completion+tool-calling API, nothing more), so planning needs its own.

Read-only by construction, not by permission check: every function here
only ever reads (forge/tools/grep.py's `read_file`/`grep_codebase`, both
path_guard-scoped to the project root already) or walks the filesystem —
there is no write path to gate, unlike forge/tools/dev_tools.py's
plan-scoped write tools.
"""
from __future__ import annotations

import fnmatch
import os

from forge.tools.grep import DEFAULT_EXCLUDES, grep_codebase, read_file
from forge.tools.path_guard import PathGuardError

_MAX_GREP_RESULTS = 50
_MAX_FIND_RESULTS = 200


def _numbered(text: str, *, start_line: int = 1) -> str:
    lines = text.splitlines()
    width = len(str(start_line + len(lines) - 1)) if lines else 1
    return "\n".join(f"{str(start_line + i).rjust(width)}\t{line}" for i, line in enumerate(lines))


def make_planner_tools(*, project_path: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Return (functions_dict, tool_schemas) pre-bound to this run's
    project root — same (functions, schemas) shape as
    forge/tools/dev_tools.py's make_dev_tools(), so both roles hand
    forge/agents/loop.py the same kind of pair."""

    def read_file_tool(path: str, offset: int | None = None, limit: int | None = None) -> str:
        try:
            content = read_file(project_path, path, offset=offset or 0, limit=limit)
        except FileNotFoundError:
            return f"File not found: {path}"
        except PathGuardError as exc:
            return f"BLOCKED: {exc}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a normal tool error
            return f"Error reading file {path}: {exc}"
        # read_file already prefixes a windowed read with "[lines a-b of n]"
        # — only add line numbers to the content itself when unwindowed, so
        # numbers always mean "line N in the whole file", never "line N of
        # this window".
        if content.startswith("[lines "):
            return content
        return _numbered(content)

    def grep_tool(pattern: str, max_results: int = _MAX_GREP_RESULTS) -> str:
        try:
            matches = grep_codebase(project_path, pattern, max_results=min(max_results, _MAX_GREP_RESULTS))
        except Exception as exc:  # noqa: BLE001 - a bad regex or missing rg is data, not a crash
            return f"Search failed: {exc}"
        if not matches:
            return "No matches."
        lines = [f"{m.file_path}:{m.line_number}: {m.line_text}" for m in matches]
        suffix = "\n... [truncated — narrow your pattern]" if len(matches) >= min(max_results, _MAX_GREP_RESULTS) else ""
        return "\n".join(lines) + suffix

    def find_files_tool(glob_pattern: str, max_results: int = _MAX_FIND_RESULTS) -> str:
        root = os.path.realpath(project_path)
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in DEFAULT_EXCLUDES]
            for name in filenames:
                rel_path = name if rel_dir == "." else f"{rel_dir}/{name}"
                if fnmatch.fnmatch(rel_path, glob_pattern) or fnmatch.fnmatch(name, glob_pattern):
                    matches.append(rel_path)
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        if not matches:
            return "No files matched."
        suffix = "\n... [truncated — narrow your pattern]" if len(matches) >= max_results else ""
        return "\n".join(sorted(matches)) + suffix

    functions = {"read_file_tool": read_file_tool, "grep_tool": grep_tool, "find_files_tool": find_files_tool}
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "read_file_tool",
                "description": (
                    "Read the contents of a file at the given path, relative to the project root. "
                    "Reads the whole file by default (up to 120,000 characters). For a large file "
                    "where you only need one part, pass offset (1-based line number) and limit "
                    "(number of lines) to read just that range."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path relative to the project root"},
                        "offset": {"type": "integer", "description": "1-based line number to start reading from. Omit to read from the start."},
                        "limit": {"type": "integer", "description": "Number of lines to read from offset. Omit to read to the end of the file."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep_tool",
                "description": (
                    "Search the codebase for a regex pattern (ripgrep syntax). Returns matching "
                    f"lines as 'path:line: text', up to {_MAX_GREP_RESULTS} results. Use this to "
                    "find where a symbol, string, or pattern is used before reading whole files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regex pattern to search for"},
                        "max_results": {"type": "integer", "description": f"Maximum matches to return (capped at {_MAX_GREP_RESULTS})"},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_files_tool",
                "description": (
                    "Find files by name/path glob pattern (e.g. '*.py', '**/test_*.py', "
                    "'lib/**/*.dart'). Use this to discover what exists before reading or "
                    "searching it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "glob_pattern": {"type": "string", "description": "Glob pattern matched against the file name or its project-relative path"},
                        "max_results": {"type": "integer", "description": f"Maximum paths to return (capped at {_MAX_FIND_RESULTS})"},
                    },
                    "required": ["glob_pattern"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    return functions, schemas
