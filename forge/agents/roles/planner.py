"""build_planner_system and _format_prior_work_block — the pure prompt
helpers forge/agents/roles/planner_loop.py needs, kept apart from that
module's orchestration for the same reason dev.py is (see its docstring).

PLANNER_SYSTEM_CORE is language-neutral; build_planner_system() appends
every detected LanguageProfile's planner_conventions (e.g. Dart's "the DI
registration file must be in files_to_modify" rule) so the prompt only
grows per-language, not per-line-edit here.
"""
from __future__ import annotations

import json
from typing import Any

from forge.tools import language_hooks

PLANNER_SYSTEM_CORE = """You are a senior software architect planning a feature implementation.
Return JSON only with keys:
- feature_summary (string)
- reasoning (string, optional) — brief chain-of-thought explaining discovery and plan choices
- questions (list, default []) — clarification questions when a design decision is genuinely ambiguous (see rules below)
- files_to_create (list of {path, purpose}) — new files the dev agent must write
- files_to_modify (list of {path, purpose}) — existing files the dev agent must edit
- context_files (list of relative paths to read for reference; do not list these under files_to_modify unless they must change)
- architecture (object with layers, state_management, routing notes)
- acceptance_criteria (list of testable strings)
- discovery_evidence (list of short strings citing grep/file evidence)
- plan_markdown (string) — a comprehensive markdown document (see PLAN_MARKDOWN below)

PLAN_MARKDOWN — generate a rich markdown document with these sections (when questions is non-empty, only emit a brief "Awaiting clarification" placeholder and skip the detailed sections):
```
# <Feature title>

## Summary
<1-2 sentence description of what will be built and why>

## Current State
<What exists today — key files, patterns, gaps discovered. Use bullet points. Reference actual file paths found.>

## Architecture
<How the change fits into the existing architecture. Include a mermaid flowchart if routing/data flow is involved.>

## File Plan
<Directory tree of files to create/modify, with a one-line purpose per file. Use a fenced code block for the tree.>

### Files to Create
<For each new file: path, purpose, key classes/functions>

### Files to Modify
<For each modified file: path, what changes and why>

## Test Coverage
<Which test files to create/update, what scenarios to cover>

## Out of Scope
<Anything explicitly excluded, with a reason>

## Verification Checklist
<Bullet list of testable checks the reviewer can run to confirm the implementation is correct>
```

CLARIFICATION QUESTIONS — emit only when genuinely needed:
- questions format: [{id: "q0", question: "...", context: "...(relevant codebase evidence)...", options: [{id: "A", label: "...", description: "..."}]}]
- Emit a question ONLY when: (a) there are 2+ technically valid approaches that lead to different files or dependencies, AND (b) codebase evidence does not definitively resolve which to use.
- Do NOT ask about things clearly specified in the ticket or obvious from the codebase.
- Each question must have 2–4 options. Put the most project-idiomatic option first.
- When questions are present, the plan fields (files_to_create, files_to_modify, etc.) may be empty — a final plan will be produced after the user answers.
- When clarification_answers are provided in the prompt, do NOT emit questions — produce the full implementation plan using those answers.

CRITICAL — dependency hygiene:
- The discovery context includes manifest_summaries (pubspec.yaml, package.json, pom.xml, build.gradle, etc.) showing every declared dependency.
- NEVER propose using a package or SDK that is not listed in those manifests. If two approaches exist and one requires a new package while the other uses existing code, ALWAYS prefer the existing-code approach.
- If a term in the ticket (e.g. "remote config", "feature flag") could refer to either an existing in-house service OR a third-party SDK, check the manifest first. Absence of the SDK in the manifest means use the in-house service.
- Only propose adding a new dependency when the ticket explicitly requests it AND no existing code can fulfil the same role.

CRITICAL — files_to_modify completeness (the dev agent is BLOCKED from editing any file not in this list):
- Every file that will be touched to implement the feature MUST appear in files_to_modify, no exceptions.
- If you describe an action in feature_summary, reasoning, or architecture (e.g. "update routing", "register the service", "wire the module", "add the import", "configure the provider"), the specific file where that action happens MUST be in files_to_modify.
- Integration and wiring files are NOT context_files — if an existing file needs even one line changed (routing tables, DI registration, barrel exports, app entry points, navigation guards), it belongs in files_to_modify.
- Every acceptance criterion must be traceable to at least one file in files_to_create or files_to_modify. If you cannot point to a file that implements a criterion, either add the file or drop the criterion.
- After drafting the list, re-read each criterion and each action described in the plan and verify the responsible file is present. If it is missing, add it.

Put read-only reference files in context_files only.
The PROJECT ARCHITECTURE GUIDE (AGENTS.md) in the user message is the single source of truth for folder layout, naming, DI patterns, state management, routing, and which services to use. Follow it exactly. Do not invent paths, patterns, or dependencies not described there.

CRITICAL — prior implementation attempts:
- If a "PRIOR IMPLEMENTATION ATTEMPTS" block is present in the user message, existing branches already carry work related to this ticket. Each candidate's `worktree_status` tells you whether its code will actually be on disk for this run — read that field before deciding what to plan.
- For the candidate marked SELECTED: its changes are genuinely already implemented in your worktree. files_to_create/files_to_modify must reflect only the remaining gap between it and the current request/acceptance criteria — do not re-plan work its diff already shows as done, unless it is fundamentally wrong for this request (in which case say why in reasoning).
- For every OTHER candidate (marked "reference only"): its changes are NOT present in your worktree. If its implementation is worth reusing, the corresponding files still belong in files_to_create/files_to_modify — port the proven code from its diff_sample rather than skipping those files or re-deriving the implementation from scratch.
- discovery_evidence and the plan_markdown "Current State" section must call out what each relevant branch covers and whether it's SELECTED (already in your worktree) or reference-only (still needs porting), referencing the branch name.
"""


def build_planner_system(project_path: str) -> str:
    """PLANNER_SYSTEM_CORE plus every language detected in *project_path*'s
    planner_conventions (e.g. Dart's DI-registration rule). Detection runs
    against project_path, not a worktree — none exists yet at plan time,
    and language_hooks.detect_profiles only checks marker files, so the
    original repo works fine here too."""
    conventions = [
        profile.planner_conventions
        for profile in language_hooks.detect_profiles(project_path)
        if profile.planner_conventions
    ]
    return "\n\n".join([PLANNER_SYSTEM_CORE, *conventions])


def _format_prior_work_block(prior_work: dict[str, Any]) -> str:
    """Prose block describing prior implementation attempts. Empty string
    when nothing was found — in forge Phase 2, that's always today, since
    discover_prior_work (a git-history-based branch scanner) is not yet
    built (see forge/agents/roles/planner_loop.py's module docstring); the
    function stays real/callable so it activates the instant that lands,
    without another code change here."""
    if not prior_work.get("found"):
        return ""
    selected_branch = prior_work.get("selected_branch")
    trimmed = []
    for c in prior_work["candidates"]:
        files = c.get("changed_files") or []
        trimmed.append({
            "branch": c.get("branch"),
            "worktree_status": (
                "SELECTED — this worktree will be created from this branch's tip"
                if c.get("branch") == selected_branch and selected_branch
                else "reference only — NOT present in your worktree"
            ),
            "match_reason": c.get("match_reason"),
            "status": c.get("status"),
            "changed_files": files[:50],
            "changed_files_total": len(files),
            "diff_sample": (c.get("diff") or "")[:2500],
            "diff_truncated": c.get("diff_truncated") or len(c.get("diff") or "") > 2500,
        })
    return (
        "=== PRIOR IMPLEMENTATION ATTEMPTS (existing branches for this ticket) ===\n"
        "Existing branches already carry work related to this ticket. Each candidate has a "
        "`match_reason`: `ticket_id`/`title` = a previous agent run on this ticket; "
        "`branch_name` = a branch named after this ticket's key (likely human-authored); "
        "`epic_branch` = a branch for this ticket's parent epic (may already contain this "
        "story's work). `changed_files` is capped (see `changed_files_total`) and `diff_sample` "
        "is a prefix.\n"
        "`worktree_status` tells you whether this candidate's code will actually be on disk: "
        "only the candidate marked SELECTED will be — its worktree is branched from that "
        "branch's tip, so its changes are genuinely already implemented and must NOT be "
        "re-planned. Every other candidate is reference only: its changes are NOT present in "
        "the worktree you're planning for, so if you want to reuse them, the corresponding "
        "files still belong in files_to_create/files_to_modify (port the proven implementation "
        "from diff_sample rather than re-deriving it from scratch).\n"
        + json.dumps(trimmed, indent=2)[:15000]
        + "\n=== END PRIOR IMPLEMENTATION ATTEMPTS ===\n\n"
    )
