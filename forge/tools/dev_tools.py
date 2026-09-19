"""Dev agent tools: plain Python functions + tool schemas. make_dev_tools()
returns (functions_dict, tool_schemas) pre-bound to a specific worktree and
plan-scoped allowed paths for a single run; forge/mcp/dev_tools_server.py
wraps these as SDK @tool-decorated MCP functions.

`_scoped_analyze` calls forge/tools/language_hooks.py's generic
`run_analyze()` dispatcher rather than importing Dart's `run_dart_analyze`
directly, so `analyze_changed_files_tool` works against whichever language
the worktree's project files declare (Dart's hook is the only one with
fine-grained error_lines today — see language_hooks.py's docstrings).
"""
from __future__ import annotations

import json
import os
import shlex
from typing import Any

from forge.tools import language_hooks
from forge.tools.docs_tool import lookup_sdk_symbol
from forge.tools.edit_file import EditFileError, edit_file
from forge.tools.filesystem import roll_back_file, snapshot_baseline, write_file
from forge.tools.grep import MAX_READ_LINES, grep_codebase, read_file
from forge.tools.language_hooks import run_analyze
from forge.tools.path_guard import PathGuardError
from forge.tools.progress import PROGRESS_STATUSES, merge_progress_update, render_progress
from forge.tools.safe_shell import run_safe_command
from forge.tools.workspace import Workspace

_RUN_COMMAND_STDOUT_TAIL = 2000
_RUN_COMMAND_STDERR_TAIL = 1000

# Tools that neither write to the worktree nor run anything that does —
# forge/agents/loop.py runs consecutive calls to these concurrently.
# analyze_changed_files_tool is deliberately NOT here: a compiler check
# can write build output (Java's target/, Dart's .dart_tool/), so two
# overlapping runs could trip over each other.
READ_ONLY_TOOLS: frozenset[str] = frozenset({"read_file_tool", "search_codebase_tool", "lookup_sdk_symbol_tool"})

# Tools whose execution counts as implementation progress for the loop's
# idle-turn budget (forge/agents/loop.py's LoopConfig.progress_tools).
#
# update_progress_tool is deliberately absent: ticking a box is bookkeeping,
# not implementation, and counting it would hand the model a way to reset
# the idle-turn budget forever without touching a file.
PROGRESS_TOOLS: frozenset[str] = frozenset({"write_file_tool", "edit_file_tool", "apply_edits_tool"})

# Tools that touch nothing on disk — they update only the run's own
# checklist, so an identical read after one is still a repeat and the
# loop's repeat-call guard must survive them (see
# forge/agents/loop.py's LoopConfig.bookkeeping_tools).
BOOKKEEPING_TOOLS: frozenset[str] = frozenset({"update_progress_tool"})


def _norm_rel(path: str) -> str:
    return os.path.normpath(str(path).strip().lstrip("/")).replace("\\", "/")


def _truncate_tail(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text or ""
    return f"...[truncated {len(text) - limit} chars]...\n{text[-limit:]}"


def _analyze_one_repo(worktree_path: str, analyze_targets: list[str]) -> dict[str, Any] | None:
    """Analyze one repo's slice of the planned files, or None when it has
    no planned file on disk yet."""
    targets = [rel for rel in analyze_targets if os.path.isfile(os.path.join(worktree_path, rel))]
    if not targets:
        return None
    result = run_analyze(worktree_path, targets)
    if result.get("skipped"):
        return {"passed": True, "skipped": True, "reason": result.get("reason", ""), "targets": targets}
    payload: dict[str, Any] = {
        "passed": result.get("passed", False),
        "error_count": result.get("error_count", 0),
        "error_lines": result.get("error_lines", [])[:25],
        "targets": targets,
    }
    if not payload["passed"] and not payload["error_lines"]:
        payload["output_tail"] = _truncate_tail(result.get("stdout") or result.get("stderr") or "", 1500)
    return payload


def _scoped_analyze(workspace: Workspace, analyze_targets: list[str]) -> str:
    """Compile/lint every repo the plan touches, each with its own
    toolchain in its own worktree.

    A story spanning a TypeScript UI and a Python API has two different
    analyzers to run, and a single combined pass would use whichever
    profile the default repo happens to declare. Partitioning by repo
    first is what makes the check mean anything for the others. Error
    lines are prefixed with the repo label so a path in the output reads
    back as a path the model can open."""
    by_label: dict[str, list[str]] = {}
    for display in analyze_targets:
        try:
            label, relative = workspace.split(display)
        except Exception:  # noqa: BLE001 — an unknown label is data, not a crash
            continue
        by_label.setdefault(label, []).append(relative)

    reports: dict[str, Any] = {}
    for label, targets in by_label.items():
        root = workspace.roots.get(label)
        if not root:
            continue
        report = _analyze_one_repo(root, targets)
        if report is not None:
            report["targets"] = [workspace.display_path(label, t) for t in report["targets"]]
            report["error_lines"] = [
                f"{workspace.display_path(label, '')}{line}" if label != workspace.default_label else line
                for line in report.get("error_lines", [])
            ]
            reports[label] = report

    if not reports:
        return "No planned source files exist yet — create them before analyzing."
    if len(reports) == 1 and workspace.default_label in reports:
        # Single-repo output shape, unchanged from before multi-repo.
        return json.dumps(reports[workspace.default_label], indent=2)
    return json.dumps(
        {
            "passed": all(r.get("passed", False) for r in reports.values()),
            "error_count": sum(r.get("error_count", 0) or 0 for r in reports.values()),
            "repos": reports,
        },
        indent=2,
    )


def _guard_run_command(
    command: str,
    worktree_path: str,
    analyze_targets: list[str],
    allow_test_run: bool,
    planned_test_files: list[str],
) -> tuple[str | None, str]:
    """Returns (block_result, command). If block_result is not None it is
    the final tool output and nothing should be executed.

    Consults every detected language profile's analyze_command_prefixes /
    test_command_prefixes / format_test_command — no language name is
    hardcoded here, so a project with no matching profile just runs the
    command as-is (safe_shell.py's allowlist is still the real gate)."""
    try:
        tokens = shlex.split(command.strip())
    except ValueError:
        return None, command

    for profile in language_hooks.detect_profiles(worktree_path):
        for prefix in profile.analyze_command_prefixes:
            if tuple(tokens[: len(prefix)]) == prefix:
                return (
                    "Redirected to analyze_changed_files_tool (full-project analyze is disabled):\n"
                    + _scoped_analyze(worktree_path, analyze_targets),
                    command,
                )
        for prefix in profile.test_command_prefixes:
            if tuple(tokens[: len(prefix)]) != prefix:
                continue
            if not allow_test_run:
                return (
                    f"BLOCKED: the approved plan contains no test files, so "
                    f"'{' '.join(prefix)}' is disabled. Use analyze_changed_files_tool instead.",
                    command,
                )
            if profile.format_test_command is None:
                return None, command
            requested_args = list(tokens[len(prefix) :])
            error, final_tokens = profile.format_test_command(worktree_path, prefix, requested_args, planned_test_files)
            if error:
                return error, command
            if final_tokens is not None:
                return None, " ".join(final_tokens)
            return None, command
    return None, command


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file_tool",
            "description": (
                f"Read up to {MAX_READ_LINES} lines of a file at the given relative path in the worktree. "
                f"Pass offset (1-based line number) and limit (number of lines, max {MAX_READ_LINES}) "
                "to read exactly the range you need — one method, one widget, one class. "
                f"A limit above {MAX_READ_LINES} is rejected. Without offset/limit you get the first "
                f"{MAX_READ_LINES} lines and a header telling you the file's total length and the "
                "offset to continue from. Use search_codebase_tool first to find the line you need."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "offset": {"type": "integer", "description": "1-based line number to start reading from. Defaults to 1."},
                    "limit": {"type": "integer", "description": f"Number of lines to read from offset (1-{MAX_READ_LINES}). Defaults to {MAX_READ_LINES}."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file_tool",
            "description": (
                "Create a NEW file with the given content. Fails if the file already exists "
                "— use edit_file_tool for existing files. Only pass overwrite=true if the "
                "file is corrupted beyond repair."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file_tool",
            "description": (
                "Surgically replace an exact block of code in an existing file. "
                "target_content must match exactly one occurrence unless allow_multiple=true. "
                "Prefer this over write_file_tool for all changes to existing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "target_content": {"type": "string", "description": "Exact code block to find"},
                    "replacement_content": {"type": "string", "description": "Replacement code"},
                    "allow_multiple": {"type": "boolean", "default": False},
                },
                "required": ["path", "target_content", "replacement_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_edits_tool",
            "description": (
                "Apply MANY surgical edits in ONE call — strongly preferred over calling "
                "edit_file_tool repeatedly. Each edit targets a file and replaces an exact "
                "block (whitespace-tolerant). Edits may span multiple files and are applied "
                "in order; if one fails the rest still apply and you get a per-edit report, "
                "so batch every independent change you know you need instead of one per turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "target_content": {"type": "string", "description": "Exact code block to find"},
                                "replacement_content": {"type": "string", "description": "Replacement code"},
                                "allow_multiple": {"type": "boolean", "default": False},
                            },
                            "required": ["path", "target_content", "replacement_content"],
                        },
                    },
                },
                "required": ["edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "roll_back_file_tool",
            "description": "Discard any local uncommitted edits to a specific file, restoring it to HEAD.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_codebase_tool",
            "description": "Search for strings or patterns across the codebase. Returns up to 15 matches.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_sdk_symbol_tool",
            "description": (
                "Look up the exact signature and docstring of an SDK/framework API "
                "(Dart/Flutter today — degrades gracefully to a 'not found' result for "
                "other languages). Use this before writing any fix for deprecated or "
                "unknown APIs. Example: query='Color.withValues' to get its exact "
                "parameter signature."
            ),
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_changed_files_tool",
            "description": (
                "Run the project's analyzer/compiler check scoped ONLY to planned files "
                "(Dart/Flutter's `analyze`, Java's `mvn compile`/`gradle compileJava`, "
                "depending on the project). Use after every meaningful change to verify "
                "compilation. Do NOT call run_command_tool for this — use this instead."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_progress_tool",
            "description": (
                "Update the PROGRESS CHECKLIST shown at the end of every message you receive. "
                "Set a planned file to 'doing' when you start it and 'done' once the edit has "
                "landed and analyze passes for it — never before. Pass several files in one "
                "call. Only paths from the approved plan can be updated; the checklist mirrors "
                "the plan and cannot be extended."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "A planned file path, exactly as the checklist shows it"},
                                "status": {"type": "string", "enum": list(PROGRESS_STATUSES)},
                            },
                            "required": ["path", "status"],
                        },
                    },
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command_tool",
            "description": (
                "Run a whitelisted shell command (e.g. 'dart run build_runner build', "
                "'mvn -q dependency:resolve'). Do NOT use for the project's analyze/compile "
                "command (e.g. 'flutter analyze', 'mvn compile') — use "
                "analyze_changed_files_tool instead. Running the project's test command is "
                "only allowed when the plan includes test files."
            ),
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        },
    },
]


# The tools whose paths may carry a repo prefix: the reads, which span
# every repo, and the writes, which reach whichever repos the approved
# plan names. The rest (analyze, run_command, rollback bookkeeping) act on
# the workspace as a whole or on the default repo, so mentioning prefixes
# in their descriptions would only mislead.
_WORKSPACE_AWARE_TOOLS = (
    "read_file_tool",
    "search_codebase_tool",
    "write_file_tool",
    "edit_file_tool",
    "apply_edits_tool",
)


def _schemas_with_workspace_note(note: str) -> list[dict[str, Any]]:
    """TOOL_SCHEMAS with the reference-repo note appended to the two tools
    that can actually read one. Returns the module-level list untouched
    when there is no note, so a single-repo run sends exactly the bytes it
    sent before this existed — the prompt prefix a provider caches."""
    if not note:
        return TOOL_SCHEMAS
    patched: list[dict[str, Any]] = []
    for schema in TOOL_SCHEMAS:
        if schema["function"]["name"] not in _WORKSPACE_AWARE_TOOLS:
            patched.append(schema)
            continue
        function = dict(schema["function"])
        function["description"] = function["description"] + note
        patched.append({**schema, "function": function})
    return patched


def make_dev_tools(
    *,
    worktree_path: str,
    allowed_paths: set[str],
    analyze_targets: list[str],
    allow_test_run: bool,
    planned_test_files: list[str],
    baseline_dir: str | None = None,
    progress_items: list[dict[str, str]] | None = None,
    default_label: str = "",
    write_roots: dict[str, str] | None = None,
    read_roots: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (functions_dict, tool_schemas) pre-bound to this run's
    worktree and plan scope.

    progress_items: the caller's live checklist (see
    forge/agents/roles/dev.py). `update_progress_tool` mutates this list
    in place, so the caller can render it as the loop's trailing reminder
    and persist it for the next fix round. None disables the tool's
    effect but still offers it, so the model never gets an unknown-tool
    error for calling something the prompt told it to call.

    write_roots: {label: worktree path} for every repo the approved plan
    writes — derived from the plan, not configured, so a story touches
    one repo or several depending on the task
    (forge/tools/workspace.py). Defaults to the single default-label
    worktree.

    read_roots: {label: path} for every repo the story may READ. Wider
    than write_roots: a repo the plan does not change is still readable,
    at its own checkout, so a UI change can be written against the API
    contract it has to match. Defaults to write_roots.

    Plan scope is still the only thing that authorises a write:
    `allowed_paths` holds the plan's paths in display form (`@api/x.py`
    for a non-default repo), and a write to anything else is refused
    whichever repo it names."""
    resolved_write = dict(write_roots or {default_label: worktree_path})
    resolved_read = dict(read_roots or resolved_write)
    # Reads see every repo; a repo with a worktree is read THERE rather
    # than at its clone, so dev reads back what it just wrote.
    resolved_read.update(resolved_write)

    write_workspace = Workspace(default_label=default_label, roots=resolved_write)
    read_workspace = Workspace(default_label=default_label, roots=resolved_read)
    workspace_note = read_workspace.describe()

    def _resolve_write(path: str) -> tuple[str, str]:
        """(worktree root, relative path) for a write. Raises for a repo
        the plan does not write, which is what stops an edit landing in a
        repo that has no branch to carry it."""
        label, relative = write_workspace.split(path)
        return write_workspace.roots[label], relative

    def _scope_error(path: str) -> str | None:
        if _norm_rel(path) in allowed_paths:
            return None
        return (
            f"BLOCKED: {path} is not in the approved plan "
            f"(allowed: {', '.join(sorted(allowed_paths)) or 'none'}). Do not modify out-of-scope files."
        )

    def read_file_tool(path: str, offset: int | None = None, limit: int | None = None) -> str:
        if limit is not None and limit > MAX_READ_LINES:
            return (
                f"BLOCKED: limit={limit} exceeds the {MAX_READ_LINES}-line cap per read. "
                f"Read the range you actually need (limit <= {MAX_READ_LINES}), or page through the file with offset."
            )
        try:
            _label, root, relative = read_workspace.root_for(path)
            return read_file(root, relative, offset=offset or 0, limit=limit, max_lines=MAX_READ_LINES)
        except Exception as exc:
            return f"Error reading file {path}: {exc}"

    def write_file_tool(path: str, content: str, overwrite: bool = False) -> str:
        blocked = _scope_error(path)
        if blocked:
            return blocked
        try:
            root, relative = _resolve_write(path)
        except PathGuardError as exc:
            return f"BLOCKED: {exc}"
        abs_target = os.path.join(root, _norm_rel(relative))
        if os.path.isfile(abs_target) and not overwrite:
            return f"BLOCKED: {path} already exists. Use edit_file_tool to change it; only pass overwrite=true if the file is corrupted beyond repair."
        try:
            snapshot_baseline(baseline_dir, root, relative)
            write_file(root, relative, content)
            return f"Successfully wrote file to {path}."
        except Exception as exc:
            return f"Error writing file {path}: {exc}"

    def edit_file_tool(path: str, target_content: str, replacement_content: str, allow_multiple: bool = False) -> str:
        blocked = _scope_error(path)
        if blocked:
            return blocked
        try:
            root, relative = _resolve_write(path)
        except PathGuardError as exc:
            return f"BLOCKED: {exc}"
        try:
            snapshot_baseline(baseline_dir, root, relative)
            edit_file(root, relative, target_content, replacement_content, allow_multiple=allow_multiple)
            return f"Successfully replaced content in {path}."
        except EditFileError as exc:
            return f"Edit failed: {exc}"
        except Exception as exc:
            return f"Error editing file {path}: {exc}"

    def apply_edits_tool(edits: list[dict[str, Any]]) -> str:
        if not edits:
            return "No edits provided."
        results: list[str] = []
        ok = 0
        for idx, spec in enumerate(edits, 1):
            path = spec.get("path", "")
            blocked = _scope_error(path)
            if blocked:
                results.append(f"[{idx}] {path}: {blocked}")
                continue
            try:
                try:
                    root, relative = _resolve_write(path)
                except PathGuardError as exc:
                    results.append(f"[{idx}] {path}: BLOCKED: {exc}")
                    continue
                snapshot_baseline(baseline_dir, root, relative)
                edit_file(root, relative, spec.get("target_content", ""), spec.get("replacement_content", ""), allow_multiple=spec.get("allow_multiple", False))
                ok += 1
                results.append(f"[{idx}] {path}: OK")
            except EditFileError as exc:
                results.append(f"[{idx}] {path}: Edit failed: {exc}")
            except Exception as exc:
                results.append(f"[{idx}] {path}: Error: {exc}")
        return f"Applied {ok}/{len(edits)} edit(s)." + "\n" + "\n".join(results)

    def roll_back_file_tool(path: str) -> str:
        blocked = _scope_error(path)
        if blocked:
            return blocked
        try:
            root, relative = _resolve_write(path)
        except PathGuardError as exc:
            return f"BLOCKED: {exc}"
        try:
            roll_back_file(root, relative, baseline_dir=baseline_dir)
            return f"Successfully rolled back uncommitted changes to {path}."
        except Exception as exc:
            return f"Error rolling back {path}: {exc}"

    def search_codebase_tool(query: str) -> str:
        hits: list[dict[str, Any]] = []
        try:
            for label, root in read_workspace.read_roots():
                if len(hits) >= 15:
                    break
                for match in grep_codebase(root, query, max_results=15 - len(hits)):
                    hits.append({
                        "file": read_workspace.display_path(label, match.file_path),
                        "line": match.line_number,
                        "text": match.line_text,
                    })
            return json.dumps(hits)
        except Exception as exc:
            return f"Error searching codebase: {exc}"

    def lookup_sdk_symbol_tool(query: str) -> str:
        try:
            return lookup_sdk_symbol(worktree_path, query)
        except Exception as exc:
            return f"Error looking up SDK symbol {query}: {exc}"

    def analyze_changed_files_tool() -> str:
        return _scoped_analyze(write_workspace, analyze_targets)

    def update_progress_tool(updates: list[dict[str, Any]]) -> str:
        if progress_items is None:
            return "Progress tracking is not enabled for this run; continue without it."
        if not updates:
            return "No updates provided.\n" + render_progress(progress_items)
        applied, rejected = merge_progress_update(progress_items, updates)
        result = f"Updated {applied} checklist entry/entries."
        if rejected:
            result += (
                f" Rejected {len(rejected)}: {', '.join(rejected[:5])}. Only planned paths can be "
                f"updated, with status one of {', '.join(PROGRESS_STATUSES)}."
            )
        return result + "\n" + render_progress(progress_items)

    def run_command_tool(command: str) -> str:
        guard_result, safe_command = _guard_run_command(command, worktree_path, analyze_targets, allow_test_run, planned_test_files)
        if guard_result is not None:
            return guard_result
        try:
            res = run_safe_command(worktree_path, safe_command)
            return f"Return Code: {res.returncode}\nSTDOUT:\n{_truncate_tail(res.stdout, _RUN_COMMAND_STDOUT_TAIL)}\nSTDERR:\n{_truncate_tail(res.stderr, _RUN_COMMAND_STDERR_TAIL)}"
        except Exception as exc:
            return f"Error running command: {exc}"

    functions: dict[str, Any] = {
        "read_file_tool": read_file_tool,
        "write_file_tool": write_file_tool,
        "edit_file_tool": edit_file_tool,
        "apply_edits_tool": apply_edits_tool,
        "roll_back_file_tool": roll_back_file_tool,
        "search_codebase_tool": search_codebase_tool,
        "lookup_sdk_symbol_tool": lookup_sdk_symbol_tool,
        "analyze_changed_files_tool": analyze_changed_files_tool,
        "update_progress_tool": update_progress_tool,
        "run_command_tool": run_command_tool,
    }

    return functions, _schemas_with_workspace_note(workspace_note)
