"""Unlike dev/planner, the verifier runs no tool-driven agent loop — it
is a deterministic gate followed by a single-shot LLM review, via
forge/services/generation.py's chat_completion_json (which goes through
forge/llm/registry.py's configured provider, same as every other role —
see that module's docstring for why it doesn't route through
forge/agents/loop.py).

The two halves are separately callable on purpose. `run_deterministic_gate`
is the **Tier 1** check forge/orchestration/graph.py's gate_node runs
straight after every dev pass, before the verifier node is ever entered:
compile/lint + the planned tests, all via subprocess, no model. If it
fails, the graph never pays for an LLM review of code that doesn't build
— `format_gate_feedback` turns the failure into one targeted user
message ("analyze failed with 2 errors in auth_service.dart — fix these")
and the graph routes straight back to dev. Only a worktree that clears
Tier 1 reaches `run_verifier`, which then reuses that same gate result
(`precomputed_gate`) rather than running the toolchain a second time.

The deterministic gate itself is a thin per-language dispatcher: for every
`LanguageProfile` forge/tools/language_hooks.py detects in the worktree,
`compare_to_plan` partitions the changed/planned files down to that
profile's slice and calls its `verify()` (Dart's bespoke pub_get ->
build_runner -> DI-check -> analyze -> test pipeline, or the shared
ensure_dependencies -> analyze -> run_tests default most languages use),
then merges the results. A repo mixing two languages just means two
profiles each verifying their own files — nothing here special-cases which
language is present.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from forge.config.store import get_int_config
from forge.services.generation import chat_completion_json
from forge.services.run_activity import append_activity
from forge.services.usage import record_phase_usage
from forge.tools import language_hooks
from forge.tools.git_tools import get_all_diffs, list_changed_files, read_file_at_head
from forge.tools.workspace import Workspace
from forge.tools.worktree_sync import sync_planned_files_from_project

logger = logging.getLogger("forge.verifier")


def _planned_paths(items: list[Any]) -> list[str]:
    paths: list[str] = []
    for item in items:
        path = item.get("path") if isinstance(item, dict) else str(item)
        if path:
            paths.append(path.lstrip("/"))
    return paths


# What a plan-coverage blocker is about, as a field rather than something to
# parse back out of its message. These are the failures where dev has simply
# not written a file the approved plan names — as opposed to an analyzer or
# test failure in code it did write — and format_gate_feedback tells dev to
# write them, not to investigate.
KIND_MISSING_NEW_FILE = "missing_new_file"  # a planned new file does not exist
KIND_UNMODIFIED_TARGET = "unmodified_target"  # a planned modification was never made
KIND_MISSING_TARGET = "missing_target"  # a planned modify target is not in the worktree
_UNWRITTEN_KINDS = (KIND_MISSING_NEW_FILE, KIND_UNMODIFIED_TARGET, KIND_MISSING_TARGET)


def _norm_declared(path: str) -> str:
    return os.path.normpath(str(path).strip().lstrip("/")).replace("\\", "/")


def _dev_target_paths(file_changes: list[dict[str, Any]] | None) -> set[str]:
    return {path.lstrip("/") for path in (_planned_paths(file_changes or []))}


def compare_to_plan(
    plan: dict[str, Any],
    worktree_path: str,
    file_changes: list[dict[str, Any]] | None = None,
    project_path: str | None = None,
    run_id: str | None = None,
    prior_verify_state: dict[str, Any] | None = None,
    declared_unchanged: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`declared_unchanged`: {path relative to this repo: reason} — planned
    files dev declared already do what the ticket needs (see dev_tools'
    declare_already_satisfied_tool). Such a file that exists and was left alone
    is not a blocker; it is reported as `assumed_satisfied` so the reviewer sees
    the claim. A declaration never excuses a file that is missing."""
    issues: list[dict[str, Any]] = []
    required_fixes: list[str] = []
    synced_from_project: list[str] = []
    assumed_satisfied: list[dict[str, str]] = []
    declared = {_norm_declared(path): reason for path, reason in (declared_unchanged or {}).items()}

    if project_path:
        synced_from_project = sync_planned_files_from_project(project_path, worktree_path, plan)
        if synced_from_project:
            logger.info("Synced %d planned file(s) from project into worktree: %s", len(synced_from_project), ", ".join(synced_from_project[:8]))

    changed_git = set(list_changed_files(worktree_path))
    changed_dev = _dev_target_paths(file_changes)
    changed = changed_git | changed_dev
    dev_targets = changed_dev

    for rel in _planned_paths(plan.get("files_to_create", [])):
        abs_path = os.path.join(worktree_path, rel)
        if not os.path.isfile(abs_path):
            msg = f"Missing planned new file: {rel}"
            issues.append({"severity": "blocker", "file": rel, "reason": msg, "kind": KIND_MISSING_NEW_FILE})
            required_fixes.append(msg)

    # Every planned file_to_modify is checked against `changed` regardless
    # of whether dev's own file_changes claims it — checking only dev's
    # self-reported targets would let an empty/inaccurate file_changes list
    # (e.g. a dev run that produced no messages at all) skip this loop
    # entirely and vacuously pass with nothing verified. `changed` already
    # includes real git diff/untracked detection, not just dev's say-so.
    for rel in _planned_paths(plan.get("files_to_modify", [])):
        abs_path = os.path.join(worktree_path, rel)
        if not os.path.isfile(abs_path):
            msg = f"Dev target missing in worktree: {rel}"
            issues.append({"severity": "blocker", "file": rel, "reason": msg, "kind": KIND_MISSING_TARGET})
            required_fixes.append(msg)
        elif rel not in changed:
            if _norm_declared(rel) in declared:
                assumed_satisfied.append({"path": rel, "reason": declared[_norm_declared(rel)]})
                continue
            msg = f"Dev did not modify target file: {rel}"
            issues.append({"severity": "blocker", "file": rel, "reason": msg, "kind": KIND_UNMODIFIED_TARGET})
            required_fixes.append(msg)
        else:
            profile = language_hooks.profile_for_path(worktree_path, rel)
            if profile and profile.check_modified_file:
                original = read_file_at_head(worktree_path, rel)
                if original:
                    try:
                        with open(abs_path, encoding="utf-8") as handle:
                            current = handle.read()
                    except OSError:
                        current = ""
                    msg = profile.check_modified_file(rel, original, current)
                    if msg:
                        issues.append({"severity": "blocker", "file": rel, "reason": msg})
                        required_fixes.append(msg)

    # Analyze prefers what dev actually touched (avoids pre-existing errors
    # in unrelated changed files bleeding into the gate); tests are always
    # restricted to the approved plan, regardless of that preference.
    planned_paths = set(_planned_paths(plan.get("files_to_create", []))) | set(_planned_paths(plan.get("files_to_modify", [])))
    by_profile = language_hooks.classify_targets_by_profile(worktree_path, changed)
    prior_verify_state = prior_verify_state or {}

    profile_reports: dict[str, Any] = {}
    coverage_gap = False
    coverage_gap_details: list[str] = []
    for profile in language_hooks.detect_profiles(worktree_path):
        analyze_all, test_all = by_profile.get(profile.key, ([], []))
        analyze_targets = [p for p in analyze_all if p in dev_targets] if dev_targets else analyze_all
        test_targets = [p for p in test_all if p in planned_paths]
        if not analyze_targets and not test_targets:
            continue

        ctx = language_hooks.VerifyContext(
            worktree_path=worktree_path,
            plan=plan,
            changed=changed,
            dev_targets=dev_targets,
            analyze_targets=sorted(analyze_targets),
            test_targets=sorted(test_targets),
            run_id=run_id,
            prior_state=prior_verify_state.get(profile.key),
        )
        result = language_hooks.run_profile_verify(profile, ctx)
        issues.extend(result.get("issues", []))
        required_fixes.extend(result.get("required_fixes", []))
        profile_reports[profile.key] = result.get("report", {})
        if result.get("coverage_gap"):
            coverage_gap = True
            if result.get("coverage_gap_detail"):
                coverage_gap_details.append(f"{profile.key}: {result['coverage_gap_detail']}")
        changed = set(list_changed_files(worktree_path)) | changed_dev  # a profile's verify may stage generated files

    return {
        "passed": len(required_fixes) == 0,
        "issues": issues,
        "required_fixes": required_fixes,
        "profiles": profile_reports,
        "changed_files": sorted(changed),
        "synced_from_project": synced_from_project,
        "dev_targets": sorted(dev_targets),
        "coverage_gap": coverage_gap,
        "coverage_gap_detail": "; ".join(coverage_gap_details) or None,
        "assumed_satisfied": assumed_satisfied,
    }


_MAX_DIFF_FILES = 20
_MAX_DIFF_CHARS = 6_000
_FAILED_OUTPUT_TAIL = 1_500

# Per-issue detail carried into the Tier 1 fix message. The analyzer's
# own error lines are the useful part and are short; a test runner's
# stdout tail is where this cap bites.
_GATE_FEEDBACK_DETAIL_CHARS = 1_500
_GATE_FEEDBACK_MAX_ISSUES = 8
# Planned files listed per group before "and N more" — the checklist dev is
# shown every turn names them all, so the message need not.
_GATE_FEEDBACK_MAX_UNWRITTEN = 20
_GATE_FEEDBACK_MAX_ERROR_LINES = 25

# `path:line` / `path:line:col` in analyzer/test output — every toolchain
# forge supports prints one of these shapes for a real failure.
_PATH_LINE_RE = re.compile(r"(?<![\w/])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z]{1,6}):\d+")


def _split_plan_by_repo(plan: dict[str, Any], workspace: Workspace) -> dict[str, dict[str, Any]]:
    """One plan per repo, each holding only that repo's files with the
    `@label/` prefix stripped.

    compare_to_plan checks planned files against ONE worktree and runs ONE
    toolchain, which is correct per repo and meaningless across them: a
    TypeScript UI and a Python API have different analyzers, and a planned
    API file simply does not exist in the UI worktree. Partitioning first
    is what lets the same gate serve a story that writes several repos."""
    per_repo: dict[str, dict[str, Any]] = {}
    for key in ("files_to_create", "files_to_modify"):
        for entry in plan.get(key) or []:
            raw = entry.get("path") if isinstance(entry, dict) else str(entry)
            if not raw:
                continue
            try:
                label, relative = workspace.split(str(raw))
            except Exception:  # noqa: BLE001 — an unknown repo surfaces as a missing planned file
                continue
            slot = per_repo.setdefault(label, {k: [] for k in ("files_to_create", "files_to_modify")})
            slot[key].append({**entry, "path": relative} if isinstance(entry, dict) else relative)
    return per_repo


def _merge_gate_reports(reports: dict[str, dict[str, Any]], workspace: Workspace) -> dict[str, Any]:
    """Fold per-repo gate reports into the single shape every caller
    already consumes, with paths and blockers re-prefixed so a failure
    names the repo it came from."""
    if len(reports) == 1 and workspace.default_label in reports:
        return reports[workspace.default_label]

    issues: list[dict[str, Any]] = []
    required_fixes: list[str] = []
    changed_files: list[str] = []
    dev_targets: list[str] = []
    profiles: dict[str, Any] = {}
    coverage_gap = False
    coverage_details: list[str] = []
    assumed_satisfied: list[dict[str, str]] = []

    for label, report in reports.items():
        prefix = "" if label == workspace.default_label else f"[{label}] "
        assumed_satisfied.extend(
            {**entry, "path": workspace.display_path(label, entry["path"])} for entry in report.get("assumed_satisfied", [])
        )
        for issue in report.get("issues", []):
            merged = dict(issue)
            merged["reason"] = f"{prefix}{issue.get('reason', '')}"
            if merged.get("file"):
                merged["file"] = workspace.display_path(label, merged["file"])
            merged["repo"] = label
            issues.append(merged)
        required_fixes.extend(f"{prefix}{fix}" for fix in report.get("required_fixes", []))
        changed_files.extend(workspace.display_path(label, p) for p in report.get("changed_files", []))
        dev_targets.extend(workspace.display_path(label, p) for p in report.get("dev_targets", []))
        for profile_key, profile_report in (report.get("profiles") or {}).items():
            profiles[f"{label}:{profile_key}"] = profile_report
        if report.get("coverage_gap"):
            coverage_gap = True
            if report.get("coverage_gap_detail"):
                coverage_details.append(f"{label}: {report['coverage_gap_detail']}")

    return {
        "passed": len(required_fixes) == 0,
        "issues": issues,
        "required_fixes": required_fixes,
        "profiles": profiles,
        "changed_files": sorted(changed_files),
        "synced_from_project": [],
        "dev_targets": sorted(dev_targets),
        "coverage_gap": coverage_gap,
        "coverage_gap_detail": "; ".join(coverage_details) or None,
        "assumed_satisfied": assumed_satisfied,
        "repos": sorted(reports),
    }


def _gate_workspace(worktree_path: str, story_worktrees: dict[str, Any] | None, default_label: str) -> Workspace:
    roots = {
        label: entry["worktree_path"]
        for label, entry in (story_worktrees or {}).items()
        if entry.get("worktree_path")
    }
    roots.setdefault(default_label, worktree_path)
    return Workspace(default_label=default_label, roots=roots)


async def run_deterministic_gate(
    *,
    plan: dict[str, Any],
    worktree_path: str,
    file_changes: list[dict[str, Any]],
    project_path: str | None = None,
    run_id: str | None = None,
    prior_build_runner: dict[str, Any] | None = None,
    story_worktrees: dict[str, Any] | None = None,
    default_label: str = "",
    declared_unchanged: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Tier 1: planned-file presence, per-language analyze/compile and the
    plan's tests — subprocesses, no model — run in EVERY repo the plan
    writes. Returns the gate report; `passed` is the routing key."""
    workspace = _gate_workspace(worktree_path, story_worktrees, default_label)
    logger.info(
        "Deterministic gate starting repos=%s dev_targets=%s",
        workspace.labels(), sorted(_dev_target_paths(file_changes)),
    )
    if run_id:
        append_activity(run_id, type="status", phase="verifier", title="Running deterministic gate (build + planned tests)")

    per_repo_plan = _split_plan_by_repo(plan, workspace) or {workspace.default_label: plan}
    per_repo_changes: dict[str, list[dict[str, Any]]] = {}
    for change in file_changes or []:
        try:
            label, relative = workspace.split(str(change.get("path") or ""))
        except Exception:  # noqa: BLE001
            continue
        per_repo_changes.setdefault(label, []).append({**change, "path": relative})

    per_repo_declared: dict[str, dict[str, str]] = {}
    for display, reason in (declared_unchanged or {}).items():
        try:
            label, relative = workspace.split(str(display))
        except Exception:  # noqa: BLE001 — a declaration for an unknown repo simply excuses nothing
            continue
        per_repo_declared.setdefault(label, {})[relative] = reason

    prior_state = prior_build_runner or {}
    reports: dict[str, dict[str, Any]] = {}
    for label, repo_plan in per_repo_plan.items():
        root = workspace.roots.get(label)
        if not root:
            continue
        # prior_build_runner is {profile: state} for a single-repo run and
        # {label: {profile: state}} once the story spans repos.
        repo_prior = prior_state.get(label) if label in prior_state else (prior_state if len(per_repo_plan) == 1 else None)
        reports[label] = await asyncio.to_thread(
            compare_to_plan, repo_plan, root, per_repo_changes.get(label, []),
            project_path=project_path if label == workspace.default_label else None,
            run_id=run_id, prior_verify_state=repo_prior, declared_unchanged=per_repo_declared.get(label),
        )

    gate = _merge_gate_reports(reports, workspace) if reports else {
        "passed": True, "issues": [], "required_fixes": [], "profiles": {},
        "changed_files": [], "synced_from_project": [], "dev_targets": [],
        "coverage_gap": False, "coverage_gap_detail": None,
    }
    if run_id:
        blockers = gate.get("required_fixes") or []
        append_activity(
            run_id, type="status", phase="verifier",
            title="Deterministic gate passed" if gate.get("passed") else f"Deterministic gate failed: {len(blockers)} blocker(s)",
            detail="; ".join(str(b) for b in blockers[:5]) or None,
        )
    return gate


def _gate_issue_details(gate: dict[str, Any], issue: dict[str, Any]) -> str:
    """The most specific text available for one gate issue: the issue's
    own `details` (analyzer error lines, test output tail), else the
    matching profile report's analyzer error lines."""
    details = str(issue.get("details") or "").strip()
    if details:
        return details
    reason = str(issue.get("reason") or "")
    profile_key = reason.split(":", 1)[0] if ":" in reason else ""
    report = (gate.get("profiles") or {}).get(profile_key) or {}
    analyze = report.get("analyze") if isinstance(report, dict) else None
    if isinstance(analyze, dict) and analyze.get("error_lines"):
        return "\n".join(str(line) for line in analyze["error_lines"][:_GATE_FEEDBACK_MAX_ERROR_LINES])
    return ""


def format_gate_feedback(gate: dict[str, Any], *, attempt: int, max_attempts: int, wrote_nothing: bool = False) -> str:
    """The user message the graph sends dev when Tier 1 fails — one
    targeted fix brief in place of a verifier review. Leads with a
    headline a weak model can act on ("2 problem(s) in auth_service.dart,
    login_page.dart"), then each blocker with its verbatim toolchain
    output, then exactly what to do next.

    Two kinds of failure need opposite instructions. An analyzer or test
    failure is in code dev wrote: fix it, minimally. A plan-coverage failure
    (a planned file never created or changed) is code dev has NOT written:
    the only fix is to write it. Told the second in the first's terms — "fix
    these specific failures", "do not rewrite files from scratch" — a model
    that had spent its turns reading went back to reading, and a run wrote
    nothing across three attempts. So the unwritten files are pulled out of
    the numbered list into their own section that names the tool for each
    and says to write now, and the closing instruction changes to match.

    `wrote_nothing`: the worktree has no changes at all. Said outright, with
    a warning on the last attempt, because in that state the model's own
    account of what it has done cannot be trusted to match the disk."""
    issues = [i for i in (gate.get("issues") or []) if isinstance(i, dict)]
    if not issues:
        issues = [{"reason": str(fix)} for fix in (gate.get("required_fixes") or [])]

    files: list[str] = []
    unwritten: dict[str, list[str]] = {kind: [] for kind in _UNWRITTEN_KINDS}
    others: list[dict[str, Any]] = []
    for issue in issues:
        kind = issue.get("kind")
        if kind in unwritten and issue.get("file"):
            unwritten[kind].append(str(issue["file"]))
            files.append(str(issue["file"]))
        else:
            others.append(issue)
    has_unwritten = any(unwritten.values())

    numbered: list[str] = []
    for idx, issue in enumerate(others[:_GATE_FEEDBACK_MAX_ISSUES], 1):
        reason = str(issue.get("reason") or "unspecified failure").strip()
        details = _gate_issue_details(gate, issue)
        if issue.get("file"):
            files.append(str(issue["file"]))
        for match in _PATH_LINE_RE.findall(details):
            files.append(match)
        if len(details) > _GATE_FEEDBACK_DETAIL_CHARS:
            details = "...\n" + details[-_GATE_FEEDBACK_DETAIL_CHARS:]
        block = f"{idx}. {reason}"
        if details:
            block += "\n" + "\n".join(f"   {line}" for line in details.splitlines() if line.strip())
        numbered.append(block)

    seen: set[str] = set()
    unique_files = [f for f in files if not (f in seen or seen.add(f))]
    where = f" in {', '.join(unique_files[:6])}" if unique_files else ""
    omitted = len(others) - len(numbered)

    lines = [f"Deterministic checks FAILED with {len(issues)} problem(s){where} (fix attempt {attempt}/{max_attempts})."]
    if wrote_nothing:
        lines.append(
            "Your last attempt wrote NO files: the worktree is unchanged, so none of the planned work exists yet."
            + (" This is your last attempt — if the next check fails, the story fails." if attempt >= max_attempts - 1 else "")
        )

    if has_unwritten:
        total = sum(len(paths) for paths in unwritten.values())
        lines += ["", f"PLANNED FILES STILL NOT WRITTEN ({total}):"]
        groups = (
            (KIND_MISSING_NEW_FILE, "Create these with write_file_tool:"),
            (KIND_UNMODIFIED_TARGET, "Change these with edit_file_tool / apply_edits_tool:"),
            (KIND_MISSING_TARGET, "These paths are not in the worktree — check the path in the plan, or create the file with write_file_tool if the plan means a new one:"),
        )
        for kind, heading in groups:
            paths = unwritten[kind]
            if not paths:
                continue
            lines.append(f"  {heading}")
            lines.extend(f"    - {path}" for path in paths[:_GATE_FEEDBACK_MAX_UNWRITTEN])
            if len(paths) > _GATE_FEEDBACK_MAX_UNWRITTEN:
                lines.append(f"    ... and {len(paths) - _GATE_FEEDBACK_MAX_UNWRITTEN} more (your PROGRESS CHECKLIST lists every planned file)")
        if numbered:
            lines += ["", "The analyzer/test runner also reported these — fix them as well:", "", *numbered]
    else:
        lines += [
            "The analyzer/test runner ran on your worktree before any review. Fix these specific failures — nothing else:",
            "",
            *numbered,
        ]
    if omitted > 0:
        lines.append(f"... and {omitted} more problem(s); run analyze_changed_files_tool to see them all.")

    if has_unwritten:
        lines.extend([
            "",
            "Your next turn must be write_file_tool / edit_file_tool / apply_edits_tool calls for these files — several independent "
            "calls in ONE turn, not one file per turn. Do not explore or re-read first: the plan is above and you have already "
            "explored; read a file only when an edit must copy its exact text. "
            "Then run analyze_changed_files_tool; when it passes for your files, respond with a plain-text summary.",
        ])
        if unwritten[KIND_UNMODIFIED_TARGET]:
            lines.extend([
                "",
                "If one of the files to change already does what the ticket needs, do not edit it: call "
                "declare_already_satisfied_tool(path, reason) with the specific reason — what the file already contains that "
                "satisfies the requirement. A comment, rename or reformat made only so this check sees a change is worse than "
                "declaring it: the reviewer sees your declaration and judges it.",
            ])
    else:
        lines.extend([
            "",
            "Apply the fixes with edit_file_tool/apply_edits_tool (do not rewrite files from scratch; do not touch files outside the plan), "
            "then run analyze_changed_files_tool. When it passes for your files, respond with a plain-text summary.",
        ])
    return "\n".join(lines)


def _build_llm_payload(plan: dict[str, Any], acceptance_criteria: list[str], file_changes: list[dict[str, Any]], diffs: list[dict[str, Any]], gate: dict[str, Any]) -> dict[str, Any]:
    left_to_a_person = plan.get("read_only_changes") or []
    plan = {k: v for k, v in plan.items() if k not in ("plan_markdown", "read_only_changes")}
    gate_summary = dict(gate)
    profiles_summary: dict[str, Any] = {}
    for profile_key, report in (gate.get("profiles") or {}).items():
        trimmed_report = dict(report)
        for sub_key, section in report.items():
            if not isinstance(section, dict):
                continue
            section = dict(section)
            keep_tail = sub_key == "test_results" and not section.get("passed", True)
            for stream in ("stdout", "stderr"):
                value = section.get(stream)
                if not value:
                    continue
                section[stream] = value[-_FAILED_OUTPUT_TAIL:] if keep_tail else ""
            trimmed_report[sub_key] = section
        profiles_summary[profile_key] = trimmed_report
    gate_summary["profiles"] = profiles_summary
    return {
        "plan": plan,
        "acceptance_criteria": acceptance_criteria,
        **({"left_to_a_person": left_to_a_person} if left_to_a_person else {}),
        "file_changes": file_changes,
        "diffs": [{"path": d.get("path"), "diff": (d.get("diff") or "")[:_MAX_DIFF_CHARS]} for d in diffs[:_MAX_DIFF_FILES]],
        "deterministic_gate": gate_summary,
    }


# A nullable-string leaf. Strict json_schema mode requires every property
# to appear in `required` at every level, so "the model may omit this" is
# expressed as a nullable type rather than as absence from `required` —
# same constraint and same workaround as the plan schema in
# forge/agents/roles/planner_loop.py.
_NULLABLE_STRING = {"type": ["string", "null"]}

# The review's shape, enforced by the provider instead of hoped for.
#
# Until this existed the verifier asked for JSON in prose and relied on
# forge/services/generation.py's tolerant extraction to find an object in
# whatever came back. That is the weakest link in the pipeline on a cheap
# model: an unparseable reply raises, and the run fails having already paid
# for the deterministic gate. Constraining the shape moves that failure
# from "sometimes, at runtime" to "never, by construction" wherever the
# provider honours json_schema.
_VERIFIER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reasoning": _NULLABLE_STRING,
        "required_fixes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One imperative sentence per fix the dev agent must apply",
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    # Nullable because a finding can be about the change as a
                    # whole rather than one file.
                    "file": _NULLABLE_STRING,
                    "reason": {"type": "string"},
                },
                "required": ["severity", "file", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["passed", "reasoning", "required_fixes", "issues"],
    "additionalProperties": False,
}

_VERIFIER_REVIEW_CORE = """You verify a software feature implementation against a plan.
Deterministic checks already passed.
Return JSON: {passed: bool, issues: [], required_fixes: [], reasoning: string optional}.
Set passed=true unless a planned acceptance criterion is clearly unmet.

An EMPTY `acceptance_criteria` list does not mean there is nothing to check — it means the plan
never said what "done" is, so "no criterion is unmet" is vacuously true and tells you nothing.
Judge such a change against `plan.feature_summary` instead, and set passed=false when you cannot
see code in the diffs that implements it.

`deterministic_gate.assumed_satisfied` lists planned files the developer declared ALREADY do what the
ticket needs, each with its reason, and left unchanged. Check every such claim against the acceptance
criteria and the code you can see: if a declared file does not actually contain what the ticket requires,
set passed=false and name it in required_fixes. Do not fail a run merely because it made few or no
changes — a file that already does the job needs none.

`left_to_a_person` lists changes in read-only repos (a shared API contract, say) that this story may
not make: a person makes them, outside this change. Never set passed=false because they are missing
from the diffs, and never ask for them in required_fixes. An acceptance criterion that depends on
them is judged only on the part this change can deliver."""


_NOTHING_TO_VERIFY = (
    "Nothing to verify: the plan named no files to create or modify, the worktree has no changes, "
    "and no file was declared already satisfied. The deterministic gate passed having checked "
    "nothing, so there is no implementation to review and no criterion this run could have met. "
    "Re-plan the story."
)


def _nothing_to_verify(plan: dict[str, Any], gate: dict[str, Any], diffs: list[dict[str, Any]]) -> bool:
    """Whether this run produced nothing a pass could possibly be about.

    Every check in compare_to_plan above is a loop over the plan's own
    files, so a plan that names none reports `passed` having verified
    nothing; the LLM review then applies "passed unless a planned
    acceptance criterion is clearly unmet" to a plan with no criteria and
    agrees. Both halves of the verifier are vacuous at once, and a story
    reaches the PR gate green with an empty diff — which is what happened,
    and why this is checked structurally rather than left to the reviewer's
    judgement.

    Deliberately narrow: it takes an empty plan AND an untouched worktree
    AND no already-satisfied declaration. A plan that names files but was
    not implemented is already a gate blocker (KIND_UNMODIFIED_TARGET), and
    a run whose only outcome is a declaration is exactly the claim the
    review exists to judge.

    forge/agents/roles/planner_loop.py's _plan_defects stops an empty plan
    at the source; this is the backstop for what never went through it — a
    run resumed from a checkpoint written before that existed, and callers
    of run_verifier outside the graph."""
    planned = (plan.get("files_to_create") or []) + (plan.get("files_to_modify") or [])
    return not planned and not gate.get("changed_files") and not gate.get("assumed_satisfied") and not diffs


def _verifier_review_system_prompt(worktree_path: str) -> str:
    conventions = [
        profile.verifier_review_conventions
        for profile in language_hooks.detect_profiles(worktree_path)
        if profile.verifier_review_conventions
    ]
    return "\n\n".join([_VERIFIER_REVIEW_CORE, *conventions])


async def run_verifier(
    *,
    plan: dict[str, Any],
    acceptance_criteria: list[str],
    worktree_path: str,
    file_changes: list[dict[str, Any]],
    project_path: str | None = None,
    run_id: str | None = None,
    prior_build_runner: dict[str, Any] | None = None,
    precomputed_gate: dict[str, Any] | None = None,
    story_worktrees: dict[str, Any] | None = None,
    default_label: str = "",
    declared_unchanged: dict[str, str] | None = None,
) -> dict[str, Any]:
    """prior_build_runner: kept as the historical param/field name for
    minimal churn to the LangGraph state schema (FeatureRunState.dev_build_runner) —
    its shape is now generic: `{profile_key: state}`, e.g.
    {"dart_flutter": {"passed": True, "signature": "..."}}.

    precomputed_gate: the Tier 1 report gate_node already produced for
    this exact worktree state (nothing runs between the gate and this
    node). Passing it skips a second, identical toolchain run — on a
    Flutter or Maven repo that is most of the verifier's wall-clock."""
    logger.info("Verifier starting worktree=%s dev_targets=%s", worktree_path, sorted(_dev_target_paths(file_changes)))
    phase_started_at = time.monotonic()
    llm_usage: dict[str, Any] | None = None
    try:
        if run_id:
            append_activity(run_id, type="status", phase="verifier", title="Verification started")
        if precomputed_gate:
            gate = precomputed_gate
        else:
            # Only reached when the verifier is called outside the graph,
            # which has already run the gate. Goes through the same
            # per-repo path so the two cannot disagree.
            gate = await run_deterministic_gate(
                plan=plan, worktree_path=worktree_path, file_changes=file_changes,
                project_path=project_path, run_id=run_id, prior_build_runner=prior_build_runner,
                story_worktrees=story_worktrees, default_label=default_label, declared_unchanged=declared_unchanged,
            )
        diffs = await asyncio.to_thread(get_all_diffs, worktree_path)

        if not gate["passed"]:
            blockers = gate.get("required_fixes") or [issue.get("reason", "") for issue in gate.get("issues", []) if issue.get("severity") == "blocker"]
            logger.warning("Verifier deterministic gate failed: %s", "; ".join(blockers[:5]) or "unknown")
            return {"verifier_report": gate, "diffs": diffs, "messages": [{"role": "verifier", "content": "Deterministic gates failed."}]}

        if _nothing_to_verify(plan, gate, diffs):
            logger.error("Verifier refusing a vacuous pass worktree=%s (empty plan, empty worktree)", worktree_path)
            if run_id:
                append_activity(run_id, type="status", phase="verifier", title="Nothing to verify — failing the run", detail=_NOTHING_TO_VERIFY)
            report = {
                "passed": False,
                "nothing_to_verify": True,
                "issues": [{"severity": "blocker", "file": None, "reason": _NOTHING_TO_VERIFY}],
                "required_fixes": [_NOTHING_TO_VERIFY],
                "deterministic_gate": gate,
            }
            return {"verifier_report": report, "diffs": diffs, "messages": [{"role": "verifier", "content": "Nothing to verify."}]}

        threshold = get_int_config("verifier_skip_llm_trivial_chars", 0)
        # A declaration that a planned file already satisfies the ticket is a
        # claim, and the review is what checks it: a run made almost entirely of
        # such claims has a tiny diff, exactly the case the skip would pass.
        if (
            threshold > 0
            and not gate.get("assumed_satisfied")
            and not (plan.get("files_to_create") or [])
            and sum(len(d.get("diff") or "") for d in diffs) <= threshold
        ):
            logger.info("Verifier passed (gate only; LLM review skipped, trivial diff)")
            report = {"passed": True, "issues": [], "required_fixes": [], "deterministic_gate": gate, "llm_review": "skipped_trivial"}
            return {"verifier_report": report, "diffs": diffs, "messages": [{"role": "verifier", "content": "Verification passed."}]}

        llm_report, llm_usage = await chat_completion_json(
            messages=[
                {"role": "system", "content": _verifier_review_system_prompt(worktree_path)},
                {"role": "user", "content": json.dumps(_build_llm_payload(plan, acceptance_criteria, file_changes, diffs, gate), indent=2)[:120000]},
            ],
            response_schema=_VERIFIER_JSON_SCHEMA,
            response_schema_name="verifier_review",
            run_id=run_id, phase="verifier", label="verifier",
        )
        if run_id and llm_report.get("reasoning"):
            append_activity(run_id, type="thinking", phase="verifier", title="Verifier reasoning", detail=str(llm_report.get("reasoning"))[:4000])
        passed = bool(llm_report.get("passed", True))
        report = {"passed": passed, "issues": llm_report.get("issues") or [], "required_fixes": llm_report.get("required_fixes") or [], "deterministic_gate": gate}
        logger.info("Verifier %s (LLM review)", "passed" if passed else "failed LLM review")
        return {"verifier_report": report, "diffs": diffs, "messages": [{"role": "verifier", "content": "Verification passed." if passed else "Verification failed."}]}
    except Exception:
        logger.exception("Verifier error worktree=%s", worktree_path)
        raise
    finally:
        # duration covers the whole phase — the deterministic build/test
        # gate plus the LLM review, not just the LLM sub-call — so "time to
        # verify" in the UI reflects where the time actually went. model is
        # only set (and cost recorded/estimated) when the LLM review
        # actually ran; a gate-only or trivial-diff-skip pass records
        # duration with no cost, not a spurious $0 "estimate".
        if run_id:
            record_phase_usage(
                run_id, phase="verifier", model=(llm_usage or {}).get("model"),
                duration_ms=int((time.monotonic() - phase_started_at) * 1000),
                api_duration_ms=(llm_usage or {}).get("api_duration_ms"),
                cost_usd=(llm_usage or {}).get("cost_usd"),
                input_tokens=(llm_usage or {}).get("input_tokens", 0),
                output_tokens=(llm_usage or {}).get("output_tokens", 0),
                cache_creation_tokens=(llm_usage or {}).get("cache_creation_tokens", 0),
                cache_read_tokens=(llm_usage or {}).get("cache_read_tokens", 0),
            )
