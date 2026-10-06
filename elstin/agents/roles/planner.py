"""build_planner_system and _format_prior_work_block — the pure prompt
helpers elstin/agents/roles/planner_loop.py needs, kept apart from that
module's orchestration for the same reason dev.py is (see its docstring).

PLANNER_SYSTEM_CORE is language-neutral; build_planner_system() appends
every detected LanguageProfile's planner_conventions (e.g. Dart's "the DI
registration file must be in files_to_modify" rule) so the prompt only
grows per-language, not per-line-edit here.
"""
from __future__ import annotations

import json
from typing import Any

from elstin.tools import language_hooks

PLANNER_SYSTEM_CORE = """You are a senior software architect planning a feature implementation.
Return JSON only with keys:
- feature_summary (string)
- reasoning (string, optional) — brief chain-of-thought explaining discovery and plan choices
- questions (list, default []) — clarification questions when a design decision is genuinely ambiguous (see rules below)
- files_to_create (list of {path, purpose}) — new files the dev agent must write
- files_to_modify (list of {path, purpose}) — existing files the dev agent must edit
- read_only_changes (list of {path, purpose}, default []) — changes the ticket needs that elstin must not make: in a READ-ONLY repo or reference folder (your prompt lists them), or a file the project guide says agents must not create or change (a generated or local-only artifact). Nothing else: work elstin can do goes under files_to_create/files_to_modify. A person makes these, and the plan's reader is told; the purpose says what the person does. Never also list such a file under files_to_create/files_to_modify. acceptance_criteria are what the other files deliver: none may require a read_only_changes file to change
- person_actions (list of {action, reason}, default []) — things the ticket's acceptance criteria lean on that NO agent here can do (see "what the dev agent can and cannot do" below): run something against data or an environment the agent cannot reach and record the result, check open pull requests or other branches, publish somewhere, get a review or sign-off. Actions, not files — a file elstin may not change is a read_only_changes entry. A person does these; the plan's reader is told, and neither the developer nor the reviewer is asked to do them
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
- Unless questions is non-empty, a plan naming NO files at all, or stating NO acceptance criteria, is not a valid answer and will be rejected. Nothing downstream can act on it: the dev agent has nothing to write and the checks that follow have nothing to check, so the ticket silently ships as an empty change.
- If the feature looks already implemented, that is not an empty plan either. Say so in reasoning, then plan whatever gap remains between the code and the acceptance criteria. For every file that already does what the ticket needs — including the existing tests that prove it — list it under files_to_modify with a purpose that starts "Already satisfied:" and says what the file already contains. The dev agent checks each such file, edits nothing, and declares it satisfied; when nothing else is left to change the story finishes as done with no change, and the epic moves on. Do not invent an edit to give the plan something to do, and do not return an empty plan or ask a question merely because the work exists. Ask only when you cannot tell whether the existing code meets the ticket.

CRITICAL — what the dev agent can and cannot do (write acceptance_criteria only the agent can meet):
- The dev agent reads, searches and edits files in the worktrees, and runs ONLY the project's own build, test and codegen commands (plus git status/diff/add/checkout) through an allowlist. It has no GitHub, Jira or Confluence access, no network, cannot see other branches or open pull requests (no git branch/log --all, no gh), and has no database or data beyond what is in the repositories and what the project's tests start themselves. The deterministic gate builds the code and runs the planned tests; the reviewer reads the diff and the gate's result.
- Every acceptance criterion is judged by that reviewer from the diff and the gate's result, and a criterion it cannot see met fails the story after three rounds. So something the agent cannot do is not an acceptance criterion for this plan:
  * Needs data, a database, an extract or an environment the agent cannot reach ("run it against the reference extract and record the differences", "confirm the nine rows", "verify against production"): write the criterion for what the change CAN deliver — the script or query is committed, can be re-run, takes its input as a parameter and fails clearly without it; the report has a place for each result, marked as awaiting capture. The run itself, and recording what it returns, is a person_actions entry. Never state a result, count or name the agent cannot have produced, and never leave a placeholder where a result belongs and call it done.
  * Needs knowledge of anything outside the checked-out repositories ("a version number free of open pull requests", "no other branch touches this file"): the agent can check the repositories on disk and nothing else. Keep the part it can do ("the version follows the highest one already in this repository") and put the outside check under person_actions.
  * Is a publication, a link, a review, a sign-off or a release note: person_actions.
  * Is about the whole project ("the complete build and every existing test pass"): leave it out. The gate already builds and runs the planned tests and the epic's QA step runs the suite; name the planned tests instead.
- Do not use person_actions to avoid work: a criterion the agent can meet from the repositories stays an acceptance criterion. When person_actions is non-empty, say in reasoning which criterion each one replaced.
- Never ask the dev agent for a comment, report row or number that would make a person_actions entry look done — that is worse than leaving it open.

CRITICAL — multi-repo projects:
- When the user message lists more than one repo, the project spans several. Paths with no prefix belong to the default repo; a path in another repo carries its `@label/` prefix, e.g. `@api/src/models/cart.py`.
- Use the SAME prefix in files_to_create/files_to_modify. That is how you say a change reaches another repo, and it is the only thing that gets the dev agent a worktree there — a file named without its prefix will be created in the wrong repo.
- Do not split one coherent change across repos unnecessarily, but do not avoid it either: if the UI change requires an API field that does not exist, plan both, each with its own prefix.
- Every repo is readable while planning, whether or not you plan to change it.

Put read-only reference files in context_files only.
The PROJECT ARCHITECTURE GUIDE (AGENTS.md) in the user message is the single source of truth for folder layout, naming, DI patterns, state management, routing, and which services to use. Follow it exactly. Do not invent paths, patterns, or dependencies not described there.

CRITICAL — prior implementation attempts:
- If a "PRIOR IMPLEMENTATION ATTEMPTS" block is present in the user message, existing branches already carry work related to this ticket. Each candidate's `worktree_status` tells you whether its code will actually be on disk for this run — read that field before deciding what to plan.
- For the candidate marked SELECTED, and any marked ALREADY MERGED: its changes are genuinely already implemented in your worktree. files_to_create/files_to_modify must reflect only the remaining gap between it and the current request/acceptance criteria — do not re-plan work its diff already shows as done, unless it is fundamentally wrong for this request (in which case say why in reasoning).
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
    """Prose block describing work that already exists for this ticket.
    Empty string when nothing was found.

    Filled from the daemon's lookup before the story's branch was made
    (elstin/services/prior_work.py, shaped by elstin/daemon/prior_work_lookup.py's
    planner_prior_work): per repo, the branches carrying the ticket's key,
    which one this worktree starts from (SELECTED — on disk), which are only
    reference, which are already in the default branch, and commits on the
    default branch that name the ticket."""
    if not prior_work.get("found"):
        return ""
    selected_branch = prior_work.get("selected_branch")
    trimmed = []
    for c in prior_work.get("candidates") or []:
        files = c.get("changed_files") or []
        selected = bool(c.get("selected")) or bool(selected_branch and c.get("branch") == selected_branch)
        in_default = str(c.get("status") or "").startswith("already in")
        entry = {
            "repo": c.get("repo"),
            "branch": c.get("branch"),
            "worktree_status": (
                "SELECTED — this worktree was created from this branch's tip" if selected
                else "ALREADY MERGED — its changes are in the default branch, so they are on disk too" if in_default
                else "reference only — NOT present in your worktree"
            ),
            "match_reason": c.get("match_reason"),
            "status": c.get("status"),
            "commits": (c.get("commits") or [])[:15],
            "changed_files": files[:50],
            "changed_files_total": c.get("changed_files_total") or len(files),
        }
        for key in ("also_carries", "pull_request"):
            if c.get(key):
                entry[key] = c[key]
        if not selected and not in_default and c.get("diff"):
            entry["diff_sample"] = c["diff"][:2500]
            entry["diff_truncated"] = bool(c.get("diff_truncated")) or len(c["diff"]) > 2500
        trimmed.append(entry)
    merged = prior_work.get("merged_commits") or {}
    return (
        "=== PRIOR IMPLEMENTATION ATTEMPTS (work that already exists for this ticket) ===\n"
        "Before this run, the repos were searched for branches carrying this ticket's key. "
        "`match_reason`: `branch_name` = a branch named after this ticket (or one of its "
        "sub-tasks), most likely a person's work in progress; `epic_branch` = a branch named "
        "after the parent epic (may already contain this story's work). `commits` and "
        "`changed_files` are capped (see `changed_files_total`); `diff_sample` is a prefix. "
        "`also_carries` names other tickets the same branch was named after — do not implement "
        "their scope here.\n"
        "`worktree_status` tells you whether a candidate's code is actually on disk: the SELECTED "
        "one is (the worktree starts from its tip), and so is an ALREADY MERGED one (its changes "
        "are in the default branch) — do not re-plan either's work; plan only what is still "
        "missing for this ticket. A reference-only candidate is NOT on disk: if it is worth "
        "reusing, its files still belong in files_to_create/files_to_modify — port the proven "
        "implementation from diff_sample rather than re-deriving it.\n"
        + json.dumps(trimmed, indent=2)[:15000]
        + (
            "\nCommits on the default branch that name this ticket (already merged, so on disk):\n"
            + json.dumps(merged, indent=2)[:3000]
            if merged else ""
        )
        + "\n=== END PRIOR IMPLEMENTATION ATTEMPTS ===\n\n"
    )
