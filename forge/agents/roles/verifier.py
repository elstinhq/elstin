"""Unlike dev/planner, the verifier runs no tool-driven agent loop — it
always runs a deterministic gate followed by a single-shot LLM review, via
forge/services/generation.py's chat_completion_json (which goes through
forge/llm/registry.py's configured provider, same as every other role —
see that module's docstring for why it doesn't route through
forge/agents/loop.py).

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
import time
from typing import Any

from forge.config.store import get_int_config
from forge.services.generation import chat_completion_json
from forge.services.run_activity import append_activity
from forge.services.usage import record_phase_usage
from forge.tools import language_hooks
from forge.tools.git_tools import get_all_diffs, list_changed_files, read_file_at_head
from forge.tools.worktree_sync import sync_planned_files_from_project

logger = logging.getLogger("forge.verifier")


def _planned_paths(items: list[Any]) -> list[str]:
    paths: list[str] = []
    for item in items:
        path = item.get("path") if isinstance(item, dict) else str(item)
        if path:
            paths.append(path.lstrip("/"))
    return paths


def _dev_target_paths(file_changes: list[dict[str, Any]] | None) -> set[str]:
    return {path.lstrip("/") for path in (_planned_paths(file_changes or []))}


def compare_to_plan(
    plan: dict[str, Any],
    worktree_path: str,
    file_changes: list[dict[str, Any]] | None = None,
    project_path: str | None = None,
    run_id: str | None = None,
    prior_verify_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    required_fixes: list[str] = []
    synced_from_project: list[str] = []

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
            issues.append({"severity": "blocker", "file": rel, "reason": msg})
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
            issues.append({"severity": "blocker", "file": rel, "reason": msg})
            required_fixes.append(msg)
        elif rel not in changed:
            msg = f"Dev did not modify target file: {rel}"
            issues.append({"severity": "blocker", "file": rel, "reason": msg})
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
    }


_MAX_DIFF_FILES = 20
_MAX_DIFF_CHARS = 6_000
_FAILED_OUTPUT_TAIL = 1_500


def _build_llm_payload(plan: dict[str, Any], acceptance_criteria: list[str], file_changes: list[dict[str, Any]], diffs: list[dict[str, Any]], gate: dict[str, Any]) -> dict[str, Any]:
    plan = {k: v for k, v in plan.items() if k != "plan_markdown"}
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
        "file_changes": file_changes,
        "diffs": [{"path": d.get("path"), "diff": (d.get("diff") or "")[:_MAX_DIFF_CHARS]} for d in diffs[:_MAX_DIFF_FILES]],
        "deterministic_gate": gate_summary,
    }


_VERIFIER_REVIEW_CORE = """You verify a software feature implementation against a plan.
Deterministic checks already passed.
Return JSON: {passed: bool, issues: [], required_fixes: [], reasoning: string optional}.
Set passed=true unless a planned acceptance criterion is clearly unmet."""


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
) -> dict[str, Any]:
    """prior_build_runner: kept as the historical param/field name for
    minimal churn to the LangGraph state schema (FeatureRunState.dev_build_runner) —
    its shape is now generic: `{profile_key: state}`, e.g.
    {"dart_flutter": {"passed": True, "signature": "..."}}."""
    logger.info("Verifier starting worktree=%s dev_targets=%s", worktree_path, sorted(_dev_target_paths(file_changes)))
    phase_started_at = time.monotonic()
    llm_usage: dict[str, Any] | None = None
    try:
        if run_id:
            append_activity(run_id, type="status", phase="verifier", title="Verification started")
        gate = await asyncio.to_thread(compare_to_plan, plan, worktree_path, file_changes, project_path=project_path, run_id=run_id, prior_verify_state=prior_build_runner)
        diffs = await asyncio.to_thread(get_all_diffs, worktree_path)

        if not gate["passed"]:
            blockers = gate.get("required_fixes") or [issue.get("reason", "") for issue in gate.get("issues", []) if issue.get("severity") == "blocker"]
            logger.warning("Verifier deterministic gate failed: %s", "; ".join(blockers[:5]) or "unknown")
            return {"verifier_report": gate, "diffs": diffs, "messages": [{"role": "verifier", "content": "Deterministic gates failed."}]}

        threshold = get_int_config("verifier_skip_llm_trivial_chars", 0)
        if threshold > 0 and not (plan.get("files_to_create") or []) and sum(len(d.get("diff") or "") for d in diffs) <= threshold:
            logger.info("Verifier passed (gate only; LLM review skipped, trivial diff)")
            report = {"passed": True, "issues": [], "required_fixes": [], "deterministic_gate": gate, "llm_review": "skipped_trivial"}
            return {"verifier_report": report, "diffs": diffs, "messages": [{"role": "verifier", "content": "Verification passed."}]}

        llm_report, llm_usage = await chat_completion_json(
            messages=[
                {"role": "system", "content": _verifier_review_system_prompt(worktree_path)},
                {"role": "user", "content": json.dumps(_build_llm_payload(plan, acceptance_criteria, file_changes, diffs, gate), indent=2)[:120000]},
            ],
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
