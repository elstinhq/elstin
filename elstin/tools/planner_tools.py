"""Read-only tools for the planner (elstin/agents/roles/planner_loop.py) —
elstin's own equivalents of the Claude Agent SDK's built-in Read/Grep/Glob,
which the planner borrowed before this migration. No provider offers
built-in tools anymore (elstin/llm/base.py's LlmProvider is a plain
completion+tool-calling API, nothing more), so planning needs its own.

Read-only by construction, not by permission check: every function here
only ever reads (elstin/tools/grep.py's `read_file`/`grep_codebase`, both
path_guard-scoped to the project root already) or walks the filesystem —
there is no write path to gate, unlike elstin/tools/dev_tools.py's
plan-scoped write tools.
"""
from __future__ import annotations

from elstin.tools.find_files import FIND_FILES_DESCRIPTION, MAX_FIND_RESULTS, find_files
from elstin.tools.grep import MAX_READ_LINES, grep_codebase, read_file
from elstin.tools.outline import OUTLINE_FILE_DESCRIPTION, outline_file
from elstin.tools.path_guard import PathGuardError
from elstin.tools.workspace import Workspace

_MAX_GREP_RESULTS = 50

# Every planner tool is read-only by construction (module docstring), so
# elstin/agents/loop.py may run any consecutive batch of them concurrently.
READ_ONLY_TOOLS: frozenset[str] = frozenset({"read_file_tool", "outline_file_tool", "grep_tool", "find_files_tool"})


def _numbered(text: str, *, start_line: int = 1) -> str:
    lines = text.splitlines()
    width = len(str(start_line + len(lines) - 1)) if lines else 1
    return "\n".join(f"{str(start_line + i).rjust(width)}\t{line}" for i, line in enumerate(lines))


def make_planner_tools(
    *, project_path: str, repo_roots: dict[str, str] | None = None, default_label: str = "",
    read_only_labels: list[str] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Return (functions_dict, tool_schemas) pre-bound to this run's
    project root — same (functions, schemas) shape as
    elstin/tools/dev_tools.py's make_dev_tools(), so both roles hand
    elstin/agents/loop.py the same kind of pair.

    repo_roots: {label: path} for EVERY repo on the project
    (elstin/tools/workspace.py). The planner reads all of them, because
    which repos a task touches is something it works out from the task
    rather than something configured ahead of it — and the plan it
    produces is what then decides where dev writes. With none supplied the
    behaviour and the prompts are byte-identical to the single-repo case."""
    roots = dict(repo_roots or {default_label: project_path})
    workspace = Workspace(default_label=default_label, roots=roots, read_only=frozenset(read_only_labels or ()))
    workspace_note = workspace.describe()

    def read_file_tool(path: str, offset: int | None = None, limit: int | None = None) -> str:
        if limit is not None and limit > MAX_READ_LINES:
            return (
                f"BLOCKED: limit={limit} exceeds the {MAX_READ_LINES}-line cap per read. "
                f"Read the range you actually need (limit <= {MAX_READ_LINES}), or page through the file with offset."
            )
        try:
            _label, root, relative = workspace.root_for(path)
            content = read_file(root, relative, offset=offset or 0, limit=limit, max_lines=MAX_READ_LINES)
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
        cap = max(1, min(max_results or _MAX_GREP_RESULTS, _MAX_GREP_RESULTS))
        lines: list[str] = []
        failures: list[str] = []
        truncated = False
        # Per root rather than one search over several paths: each root
        # needs its own relative paths and its own label prefix, and the
        # primary repo's hits lead because that is the one being changed.
        for label, root in workspace.read_roots():
            remaining = cap - len(lines)
            if remaining <= 0:
                truncated = True
                break
            try:
                # One more than fits, so a cut list is known to be cut.
                matches = grep_codebase(root, pattern, max_results=remaining + 1)
            except Exception as exc:  # noqa: BLE001 - a bad regex or missing rg is data, not a crash
                failures.append(f"{label}: {exc}")
                continue
            if len(matches) > remaining:
                truncated = True
                matches = matches[:remaining]
            lines.extend(
                f"{workspace.display_path(label, m.file_path)}:{m.line_number}: {m.line_text}" for m in matches
            )
        if not lines:
            return "Search failed: " + "; ".join(failures) if failures else "No matches."
        suffix = "\n... [truncated — narrow your pattern]" if truncated else ""
        return "\n".join(lines) + suffix

    def outline_file_tool(path: str) -> str:
        try:
            _label, root, relative = workspace.root_for(path)
            return outline_file(root, relative)
        except FileNotFoundError:
            return f"File not found: {path}"
        except PathGuardError as exc:
            return f"BLOCKED: {exc}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a normal tool error
            return f"Error outlining {path}: {exc}"

    def find_files_tool(glob_pattern: str, max_results: int = MAX_FIND_RESULTS) -> str:
        return find_files(workspace, glob_pattern, max_results)

    functions = {
        "read_file_tool": read_file_tool,
        "outline_file_tool": outline_file_tool,
        "grep_tool": grep_tool,
        "find_files_tool": find_files_tool,
    }
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "read_file_tool",
                "description": (
                    f"Read up to {MAX_READ_LINES} lines of a file at the given path, relative to the project root. "
                    f"Pass offset (1-based line number) and limit (number of lines, max {MAX_READ_LINES}) to read "
                    f"exactly the range you need. A limit above {MAX_READ_LINES} is rejected. Without offset/limit "
                    f"you get the first {MAX_READ_LINES} lines plus a header with the file's total length and the "
                    "offset to continue from. Prefer outline_file_tool (a file's declarations with line numbers) or "
                    "grep_tool to locate the lines that matter before reading."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path relative to the project root"},
                        "offset": {"type": "integer", "description": "1-based line number to start reading from. Defaults to 1."},
                        "limit": {"type": "integer", "description": f"Number of lines to read from offset (1-{MAX_READ_LINES}). Defaults to {MAX_READ_LINES}."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "outline_file_tool",
                "description": OUTLINE_FILE_DESCRIPTION + workspace_note,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Path relative to the project root"}},
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
                    "find where a symbol, string, or pattern is used before reading whole files." + workspace_note
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
                "description": FIND_FILES_DESCRIPTION + workspace_note,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "glob_pattern": {"type": "string", "description": "Glob pattern matched against the file name or its project-relative path"},
                        "max_results": {"type": "integer", "description": f"Maximum paths to return (capped at {MAX_FIND_RESULTS})"},
                    },
                    "required": ["glob_pattern"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    return functions, schemas
