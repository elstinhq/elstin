"""build_dev_system and the prompt/plan-scope helper functions
forge/agents/roles/dev_loop.py actually needs.

Deliberately split this way: everything here is pure (prompt text, plan
path-set math, diff/finalize helpers) and testable with no provider, no
event loop and no DB, while dev_loop.py owns the orchestration that talks
to forge/agents/loop.py. The same split holds for planner.py /
planner_loop.py; verifier.py and qa.py are single modules only because
they're small enough not to need it.

DEV_SYSTEM_LOOP_CORE is language-neutral; build_dev_system() appends every
detected LanguageProfile's dev_conventions (e.g. Dart's deprecated-API and
constructor-pattern rules) — a new language adds a convention block, never
an edit to this one.
"""
from __future__ import annotations

import os
from typing import Any

from forge.tools import language_hooks
from forge.tools.git_tools import list_changed_files, stage_files
from forge.tools.grep import read_file
from forge.tools.workspace import Workspace

DEV_SYSTEM_LOOP_CORE = """You are a senior software developer implementing an approved plan in a git worktree.
You work iteratively by calling tools. Analyze your progress after each tool result and decide what to do next.
When the implementation is complete and all files compile cleanly, respond with a plain-text summary of everything
you created and modified — do not call any more tools.

CRITICAL — Issue independent tool calls TOGETHER in a single turn:
- You can return SEVERAL tool calls in one response. Any calls that do not depend on each other's results MUST go out together.
- Creating planned files: when the plan lists several files to create and you know what goes in them, issue ALL those "write_file" calls in ONE turn. Do NOT write one file per turn.
- Exploring: issue all the "outline_file" / "read_file" / "search_codebase" / "find_files" calls you need at once rather than one at a time.
- To see what is in a folder or find a file by name, use "find_files" (e.g. 'some/folder/*' or '**/*.ext'). The shell ("run_command") cannot list, find or read files and takes no pipes — do not try ls, find, grep or cat there.
- Only split across turns when a call genuinely needs an earlier call's result (e.g. read a file, then edit it based on what you read).
- Every extra turn re-sends the entire conversation so far, so one turn with five calls costs far less than five turns with one call.

Rules:
- Avoid overwriting entire files using "write_file" for small changes. Use "edit_file" instead to perform precise modifications.
- BATCH your edits: when you already know several edits you need to make (across one or more files), issue them together in a SINGLE "apply_edits" call instead of one "edit_file" call per turn. This is much faster. Only fall back to a single "edit_file" when an edit depends on the result of a previous one.
- HOWEVER, if a file becomes severely corrupted (e.g., edits keep failing because the code is duplicated or mangled), you SHOULD use "write_file" to rewrite the entire file with the correct content.
- Ensure "target_content" matches a block in the file. Whitespace differences (trailing spaces, indentation) are tolerated, but the block must be unique unless you pass allow_multiple.
- Verify your code compiles with the "analyze_changed_files" tool after a batch of changes — you do NOT need to analyze after every single edit.
- If compile errors are found, fix them iteratively or rollback files to undo mistakes using "roll_back_file".
- "read_file" serves at most 250 lines per call. Before reading a file you have not seen, call "outline_file" — its classes and methods with line numbers — or "search_codebase" to find the lines you need, then pass "offset"/"limit" to read just that range; page with the offset the response header gives you.
- Never re-issue a tool call with the same arguments as your previous turn — the repeated call is rejected. If the last result was not enough, change what you ask for.
- Every turn should move the implementation forward. After a few turns of only reading and searching, start editing; a run that keeps reading without editing is stopped early.
- If a file the plan says to MODIFY already does what the ticket needs, do not edit it: call "declare_already_satisfied" with the specific reason (what the file already contains). NEVER add a comment, rename or reformat just to make a planned file count as changed — that is worse than declaring it satisfied, and the reviewer sees your declaration and checks it. Only files the plan modifies qualify; a file to create must be written.

CRITICAL — the progress checklist:
- A PROGRESS CHECKLIST appears at the end of every message you receive. It is the authoritative record of what remains, and it is yours to maintain.
- Call "update_progress" the moment a file's state changes: to "doing" when you start on it, to "done" once it is written AND analyze passes for it. Batch several files into one call.
- Do NOT mark a file done because you intend to write it. Done means the edit landed.
- Updating the checklist is bookkeeping, not progress. It does not substitute for editing files, and a run that only updates its checklist is still treated as making no progress.
- When every entry is done and analyze passes, stop calling tools and reply with your plain-text summary.

CRITICAL — Scope discipline (ENFORCED: the tool layer rejects out-of-scope writes/edits and blocked commands):
- ONLY create or modify files that are listed in the approved plan (files_to_create, files_to_modify).
- Do NOT edit test files, config files, or any other files unless the plan explicitly requires it.
- If the analyzer or test runner reports errors in files OUTSIDE your plan scope, IGNORE those errors. They are pre-existing and not your responsibility.
- If a pre-existing test fails, do NOT try to fix it. Only address errors in YOUR planned files.

CRITICAL — Testing:
- Running the project's test command is BLOCKED unless the approved plan includes test files; when allowed, it runs only the planned test files.
- Use "analyze_changed_files" (not the raw test command) to verify compilation.

CRITICAL — Fix iterations:
- If the user message contains "FIX MODE", the implementation ALREADY EXISTS (current contents are provided in the message).
- Do NOT rewrite files from scratch. Apply ONLY the listed required fixes using "edit_file".
- After applying the fixes, run "analyze_changed_files", then complete.
"""


def build_dev_system(worktree_path: str) -> str:
    """DEV_SYSTEM_LOOP_CORE plus every language detected in *worktree_path*'s
    dev_conventions (e.g. Dart's deprecated-API and constructor-pattern
    rules)."""
    conventions = [
        profile.dev_conventions
        for profile in language_hooks.detect_profiles(worktree_path)
        if profile.dev_conventions
    ]
    return "\n\n".join([DEV_SYSTEM_LOOP_CORE, *conventions])


def _format_skills_block(skills: list[dict[str, str]] | None) -> str:
    if not skills:
        return ""
    lines = ["Agent skills:"]
    for skill in skills:
        name = skill.get("name") or skill.get("path") or "skill"
        content = skill.get("content") or ""
        lines.append(f"\n### {name}\n{content}")
    return "\n".join(lines) + "\n\n"


_FIX_MODE_FILE_CAP = 8_000
_FIX_MODE_MAX_FILES = 4

def _norm_rel(path: str) -> str:
    return os.path.normpath(str(path).strip().lstrip("/")).replace("\\", "/")


def _plan_path_set(plan: dict[str, Any]) -> set[str]:
    """Relative paths the dev agent is allowed to create or modify."""
    paths: set[str] = set()
    for key in ("files_to_create", "files_to_modify"):
        for item in plan.get(key) or []:
            rel = item.get("path") if isinstance(item, dict) else str(item)
            if rel:
                paths.add(_norm_rel(rel))
    return paths


def _collect_required_fixes(verifier_report: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """Flatten a failed verifier report into (required_fixes, analyze_error_lines)."""
    if not verifier_report or verifier_report.get("passed", True):
        return [], []
    gate = verifier_report.get("deterministic_gate") or verifier_report
    analyze = gate.get("analyze") or {}
    error_lines = [str(line) for line in (analyze.get("error_lines") or [])]
    seen: set[str] = {line.strip() for line in error_lines}
    fixes: list[str] = []
    sources = [
        verifier_report.get("required_fixes") or [],
        gate.get("required_fixes") or [],
        [issue.get("reason") if isinstance(issue, dict) else issue for issue in (verifier_report.get("issues") or [])],
    ]
    for source in sources:
        for item in source:
            text = str(item).strip() if item else ""
            if text and text not in seen:
                seen.add(text)
                fixes.append(text)
    return fixes, error_lines


def test_run_instruction(test_commands: list[tuple[str | None, str]] | None) -> str:
    """The sentence that sends dev to run the plan's tests itself, or "" when
    the plan has none. `test_commands`: (repo to name or None for the default
    repo, command) for each repo that holds planned tests.

    A fix brief used to end "then run analyze_changed_files_tool", a tool that
    compiles and lints and never runs a test — so a developer sent back for
    missing coverage, or a failing test, finished each round on "analyzer
    passed" with the tests unrun. The tests are allowed through
    run_command_tool, in the repo that holds them."""
    if not test_commands:
        return ""
    ways = []
    for repo, command in test_commands:
        what = f"`{command}`" if command else "the project's test command"
        ways.append(f'{what} with repo="{repo}"' if repo else what)
    return (
        "Run the plan's tests yourself before you finish — run_command_tool with " + " or ".join(ways) + ". "
        "analyze_changed_files_tool only compiles and lints; it does NOT run tests, so a clean analyze says nothing "
        "about them. "
    )


def _build_iteration_brief(
    worktree_path: str,
    allowed_paths: set[str],
    required_fixes: list[str],
    analyze_errors: list[str],
    continuing: bool = False,
    test_commands: list[tuple[str | None, str]] | None = None,
) -> str:
    """Task brief for the dev loop. First iteration: implement. Retry
    iterations: FIX MODE — an explicit fix checklist."""
    run_tests = test_run_instruction(test_commands)
    if not required_fixes and not analyze_errors:
        return "Begin implementation. Analyze your options, call tools, and verify your changes with analyze_changed_files iteratively."

    lines = [
        "FIX MODE — the implementation from the previous iteration ALREADY EXISTS in the worktree.",
        "Do NOT rewrite files from scratch. Apply ONLY the fixes below (batch them with apply_edits).",
        "",
        "Required fixes:",
    ]
    lines.extend(f"{idx}. {fix}" for idx, fix in enumerate(required_fixes, 1))
    if analyze_errors:
        lines.append("")
        lines.append("Analyzer errors (verbatim):")
        lines.extend(f"- {line}" for line in analyze_errors[:25])

    if continuing:
        lines.append("")
        lines.append(
            "You already have the earlier context from this session. Re-read a file "
            "with read_file_tool only if you need to confirm its current on-disk state, "
            "then apply the fixes and run analyze_changed_files. " + run_tests + "When every fix is "
            "applied and the checks pass for your files, respond with a plain-text summary."
        )
        return "\n".join(lines)

    existing = [rel for rel in sorted(allowed_paths) if os.path.isfile(os.path.join(worktree_path, rel))]
    feedback_text = "\n".join([*required_fixes, *analyze_errors]).lower()
    referenced = [rel for rel in existing if rel.lower() in feedback_text or os.path.basename(rel).lower() in feedback_text]
    to_inject = (referenced or existing)[:_FIX_MODE_MAX_FILES]
    if to_inject:
        lines.append("")
        lines.append("Current contents of your planned files:")
        for rel in to_inject:
            try:
                content = read_file(worktree_path, rel)
            except Exception:
                continue
            if len(content) > _FIX_MODE_FILE_CAP:
                content = content[:_FIX_MODE_FILE_CAP] + "\n...[file truncated]"
            lines.append(f"\n### {rel}\n```dart\n{content}\n```")
        skipped = len(existing) - len(to_inject)
        if skipped > 0:
            lines.append(f"\n({skipped} other planned file(s) exist; use read_file_tool if needed)")

    lines.append("")
    lines.append(
        "Apply each fix with edit_file_tool, then run analyze_changed_files_tool. " + run_tests +
        "When every fix is applied and the checks pass for your files, respond with a plain-text summary."
    )
    return "\n".join(lines)


def _finalize_dev_changes(
    write_roots: dict[str, str], plan: dict[str, Any], default_label: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage changed files and run any post-edit codegen step each detected
    language's profile needs (e.g. Dart's build_runner — most languages,
    including Java, don't have one), in EVERY repo the plan writes.

    Returns (file_changes, dev_verify_state). file_changes carries paths in
    display form (`@api/x.py` for a non-default repo) so the verifier and
    the gate can route each one back to the repo it belongs to.
    dev_verify_state is `{repo_label: {profile_key: state}}` when the story
    spans repos, and the historical `{profile_key: state}` when it does not
    — a single-repo run's shape is unchanged."""
    workspace = Workspace(default_label=default_label, roots=dict(write_roots))
    file_changes: list[dict[str, Any]] = []
    per_repo_state: dict[str, Any] = {}

    create_paths = {
        _norm_rel(f.get("path") if isinstance(f, dict) else str(f))
        for f in plan.get("files_to_create", [])
        if (f.get("path") if isinstance(f, dict) else f)
    }

    for label, root in workspace.read_roots():
        changed_files = list_changed_files(root)
        if not changed_files:
            continue
        stage_files(root, changed_files)

        repo_state: dict[str, Any] = {}
        for profile in language_hooks.detect_profiles(root):
            if profile.after_dev_changes is None:
                continue
            repo_state[profile.key] = profile.after_dev_changes(root, changed_files)
        if repo_state:
            per_repo_state[label] = repo_state

        for path in changed_files:
            display = workspace.display_path(label, path)
            action_type = "create" if _norm_rel(display) in create_paths else "modify"
            file_changes.append(
                {"path": display, "action": action_type, "summary": "Updated iteratively by agentic dev loop"}
            )

    if len(workspace.roots) <= 1:
        return file_changes, per_repo_state.get(default_label, {})
    return file_changes, per_repo_state
