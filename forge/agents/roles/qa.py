"""The epic-level QA role.

QA does not run per story. It runs once, as its own ticket, after every
implementation story in the epic has been merged into the integration
branch — which is the first moment at which the epic's changes coexist and
"does the feature actually work end to end" is a question that can be
asked at all. A per-story QA pass could never answer it: story 3 of 5 has
no idea what stories 4 and 5 will do.

That timing is what makes QA distinct from the verifier rather than a
duplicate of it. The verifier is per story, scoped to that story's own
planned files, and gates whether the story may open a PR. QA is per epic,
scoped to everything, and gates nothing — it reports. Concretely, two
things here have no counterpart anywhere else in the pipeline:

1. `run_full_test_suites` runs the WHOLE suite on the merged branch. Every
   per-story verifier run deliberately tests only the files that story's
   plan named, so a test that story 2 breaks in a file story 5 owns is
   invisible to both of their verifier runs. Nothing else ever runs the
   combined suite.
2. The review reads the cumulative diff against *every* story's acceptance
   criteria at once, so gaps that fall between stories — the handoff
   nobody's ticket owned — have somewhere to surface.

The report is posted to the QA ticket as a JIRA comment (see
forge/daemon/main.py's `_post_qa_report`). That is deliberate: an earlier
version of this role wrote `qa_report` into graph state and nothing ever
read it, which made the whole stage cost without output.

Widget-tree extraction (a Flutter-widget-specific AST parser feeding
supplementary context for `_page.dart`/`_screen.dart` files into the review
prompt) is intentionally not included. Skipping it doesn't change what QA
checks, just removes one optional context signal.
"""
from __future__ import annotations

import json
import time
from typing import Any

from forge.services.generation import chat_completion_json
from forge.services.run_activity import append_activity
from forge.services.usage import record_phase_usage
from forge.tools import language_hooks

_NULLABLE_STRING = {"type": ["string", "null"]}

_STRING_LIST = {"type": "array", "items": {"type": "string"}}

# The report's shape, enforced by the provider rather than requested in
# prose — same reasoning as the verifier's schema, and it matters more
# here: forge/agents/roles/qa.py's format_qa_report_comment is this
# report's only consumer and it reads specific keys, so a reply that
# parses but uses different key names produces an empty JIRA comment
# rather than a visible failure.
QA_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "pass_with_findings", "fail"]},
        "summary": {"type": "string"},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "acceptance_criterion": {"type": "string"},
                    # Nullable: a gap can fall between stories rather than
                    # inside one, which is exactly what epic QA is for.
                    "story_key": _NULLABLE_STRING,
                    "finding": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                },
                "required": ["acceptance_criterion", "story_key", "finding", "severity"],
                "additionalProperties": False,
            },
        },
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scenario_name": {"type": "string"},
                    "flow_type": {"type": "string"},
                    "steps": _STRING_LIST,
                    "verification_points": _STRING_LIST,
                },
                "required": ["scenario_name", "flow_type", "steps", "verification_points"],
                "additionalProperties": False,
            },
        },
        "regression_scope": _STRING_LIST,
        "manual_checklist": _STRING_LIST,
    },
    # full_test_suite is deliberately absent: the deterministic suite result
    # is stamped onto the report after parsing (evidence beats opinion), so
    # asking the model for it would invite it to invent one.
    "required": ["verdict", "summary", "gaps", "scenarios", "regression_scope", "manual_checklist"],
    "additionalProperties": False,
}

QA_SYSTEM = """You are a QA architect performing independent end-to-end validation of a completed epic.

Every implementation story in the epic has been merged onto one branch. You are reviewing the combined result against what the stories collectively promised — not reviewing any single story.

You are given:
- The QA ticket itself: what this validation is being asked to cover.
- The epic and each implementation story, with its acceptance criteria.
- The cumulative diff of the integration branch against its base.
- The result of running the project's full test suite on the merged branch.

Focus on what only an epic-wide view can reveal:
- Acceptance criteria that no diff appears to satisfy — work that was planned but is not there.
- Seams between stories: a contract one story produces and another consumes, where the two do not line up.
- Regressions in behaviour that no single story owned.
- States and edge cases that the individual stories each assumed someone else handled.

Do not re-report ordinary code-style nits, and do not repeat compile or test failures that the test-suite result already states — cite them only where they change your assessment of a criterion.

Return JSON with keys:
- verdict: one of "pass", "pass_with_findings", "fail"
- summary: 2-4 sentences on the state of the epic as a whole
- gaps: list of {acceptance_criterion, story_key, finding, severity} where severity is "blocker" | "major" | "minor" — acceptance criteria that appear unmet or only partly met
- scenarios: list of {scenario_name, flow_type, steps, verification_points} — end-to-end flows worth exercising
- regression_scope: list of strings — areas outside the epic that these changes could plausibly affect
- manual_checklist: list of strings — checks that require a human, device, or environment
"""

_MAX_DIFF_CHARS = 60_000
_MAX_STORIES = 40
_MAX_PAYLOAD_CHARS = 160_000


def _test_suite_digest(result: dict[str, Any]) -> dict[str, Any]:
    """Trims the full-suite result down to what the reviewer can act on —
    the raw stdout of a whole test run is far too large to put in a prompt,
    and the failure lines are the only part that carries signal."""
    if not result.get("ran"):
        return {"ran": False, "reason": result.get("reason", "unknown")}

    per_profile = {}
    for key, sub in (result.get("profiles") or {}).items():
        per_profile[key] = {
            "passed": sub.get("passed", False),
            "tests_run": sub.get("tests_run"),
            "error_count": sub.get("error_count"),
            "failures": (sub.get("error_lines") or [])[:25],
            "timed_out": sub.get("timed_out", False),
            # No error_lines means the toolchain gave us no structured
            # failures (a build error rather than a test failure, say), so
            # fall back to the tail of stderr rather than reporting nothing.
            "stderr_tail": (sub.get("stderr") or "")[-2000:] if not sub.get("error_lines") else "",
        }
    return {"ran": True, "passed": result.get("passed", False), "profiles": per_profile}


async def run_epic_qa(
    *,
    qa_story: dict[str, Any],
    epic: dict[str, Any],
    dev_stories: list[dict[str, Any]],
    branch_diff: dict[str, Any],
    other_repo_diffs: list[dict[str, Any]] | None = None,
    worktree_path: str = "",
    project_path: str,
    run_id: str | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    """`qa_story` is the validation ticket being worked; `dev_stories` are
    the epic's implementation stories in wave order; `branch_diff` is
    `git_tools.get_branch_diff_summary` over the integration branch."""
    phase_started_at = time.monotonic()
    llm_usage: dict[str, Any] | None = None
    try:
        if run_id:
            append_activity(run_id, type="status", phase="qa", title="Epic QA started")
            append_activity(run_id, type="status", phase="qa", title="Running full test suite on merged branch")

        suite_result = language_hooks.run_full_test_suites(worktree_path)
        suite = _test_suite_digest(suite_result)

        if run_id:
            state = "not run" if not suite["ran"] else ("passed" if suite["passed"] else "FAILED")
            append_activity(run_id, type="status", phase="qa", title=f"Full test suite {state}")

        payload = {
            "qa_ticket": {
                "key": qa_story.get("jira_key"),
                "summary": qa_story.get("summary"),
                "description": qa_story.get("description"),
            },
            "epic": {
                "key": epic.get("jira_key"),
                "summary": epic.get("summary"),
                "description": epic.get("description"),
            },
            "implementation_stories": [
                {
                    "key": s.get("jira_key"),
                    "summary": s.get("summary"),
                    "description": s.get("description"),
                    "status": s.get("status"),
                }
                for s in dev_stories[:_MAX_STORIES]
            ],
            "full_test_suite": suite,
            "cumulative_diff": {
                "base": branch_diff.get("base"),
                "branch": branch_diff.get("branch"),
                "changed_files": branch_diff.get("changed_files", []),
                "diff": (branch_diff.get("diff") or "")[:_MAX_DIFF_CHARS],
                "diff_truncated": branch_diff.get("diff_truncated", False),
            },
        }

        # A cross-repo epic: the same integration branch in each repo its
        # stories wrote to. Present only when there is more than one, so a
        # single-repo epic's payload is byte-identical to before.
        if other_repo_diffs:
            payload["other_repo_diffs"] = [
                {
                    "repo": d.get("repo"),
                    "base": d.get("base"),
                    "branch": d.get("branch"),
                    "changed_files": d.get("changed_files", []),
                    "diff": (d.get("diff") or "")[:_MAX_DIFF_CHARS],
                    "diff_truncated": d.get("diff_truncated", False),
                }
                for d in other_repo_diffs
            ]

        qa_report, llm_usage = await chat_completion_json(
            messages=[
                {"role": "system", "content": QA_SYSTEM},
                {"role": "user", "content": json.dumps(payload, indent=2)[:_MAX_PAYLOAD_CHARS]},
            ],
            response_schema=QA_JSON_SCHEMA,
            response_schema_name="epic_qa_report",
            run_id=run_id,
            phase="qa",
            label="epic_qa",
            project_id=project_id,
        )

        # The deterministic suite result outranks the model's own verdict: if
        # the merged branch does not build or its tests fail, that is a fail
        # regardless of how the review read the diff. Evidence beats opinion,
        # the same principle the verifier's gate applies per story.
        if suite["ran"] and not suite["passed"]:
            qa_report["verdict"] = "fail"
        qa_report["full_test_suite"] = suite

        if run_id:
            append_activity(
                run_id, type="status", phase="qa",
                title=f"QA report generated (verdict: {qa_report.get('verdict', 'unknown')})",
            )

        return {
            "qa_report": qa_report,
            "messages": [{"role": "qa", "content": f"Epic QA complete: {qa_report.get('verdict', 'unknown')}."}],
        }
    finally:
        # duration covers the whole phase — the full test suite run plus
        # the LLM review — not just the LLM sub-call.
        if run_id:
            record_phase_usage(
                run_id, phase="qa", model=(llm_usage or {}).get("model"),
                duration_ms=int((time.monotonic() - phase_started_at) * 1000),
                api_duration_ms=(llm_usage or {}).get("api_duration_ms"),
                cost_usd=(llm_usage or {}).get("cost_usd"),
                input_tokens=(llm_usage or {}).get("input_tokens", 0),
                output_tokens=(llm_usage or {}).get("output_tokens", 0),
                cache_creation_tokens=(llm_usage or {}).get("cache_creation_tokens", 0),
                cache_read_tokens=(llm_usage or {}).get("cache_read_tokens", 0),
            )


_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}


def format_qa_report_comment(report: dict[str, Any], *, epic_key: str) -> str:
    """Renders the report as JIRA-flavoured markdown for posting onto the QA
    ticket. This is the report's only consumer, so it is written for a
    person reading the ticket rather than for a machine — findings first,
    ordered by severity, because that's what a reader acts on."""
    verdict = str(report.get("verdict", "unknown")).replace("_", " ")
    lines = [
        f"*Automated epic QA — {epic_key}*",
        "",
        f"*Verdict:* {verdict.upper()}",
        "",
        str(report.get("summary", "")).strip(),
        "",
    ]

    suite = report.get("full_test_suite") or {}
    if not suite.get("ran"):
        lines += ["*Full test suite:* not run — " + str(suite.get("reason", "unsupported for this project")), ""]
    else:
        lines += [f"*Full test suite:* {'passed' if suite.get('passed') else 'FAILED'} on the merged branch"]
        for key, sub in (suite.get("profiles") or {}).items():
            detail = f"  - {key}: {'passed' if sub.get('passed') else 'failed'}"
            if sub.get("tests_run") is not None:
                detail += f" ({sub['tests_run']} tests"
                if sub.get("error_count"):
                    detail += f", {sub['error_count']} failing"
                detail += ")"
            lines.append(detail)
            for failure in (sub.get("failures") or [])[:10]:
                lines.append(f"    - {failure}")
        lines.append("")

    gaps = sorted(
        report.get("gaps") or [],
        key=lambda g: _SEVERITY_ORDER.get(str(g.get("severity", "minor")).lower(), 3),
    )
    if gaps:
        lines += ["*Gaps against acceptance criteria:*"]
        for gap in gaps:
            severity = str(gap.get("severity", "minor")).upper()
            story = gap.get("story_key") or "—"
            lines.append(f"  - [{severity}] ({story}) {gap.get('acceptance_criterion', '')} — {gap.get('finding', '')}")
        lines.append("")
    else:
        lines += ["*Gaps against acceptance criteria:* none identified", ""]

    scenarios = report.get("scenarios") or []
    if scenarios:
        lines += ["*End-to-end scenarios to exercise:*"]
        for scenario in scenarios:
            lines.append(f"  - {scenario.get('scenario_name', '')} ({scenario.get('flow_type', 'flow')})")
        lines.append("")

    checklist = report.get("manual_checklist") or []
    if checklist:
        lines += ["*Manual checks:*"] + [f"  - {item}" for item in checklist] + [""]

    regression = report.get("regression_scope") or []
    if regression:
        lines += ["*Regression scope:*"] + [f"  - {item}" for item in regression] + [""]

    lines += ["_Generated by forge. This is an assistive review, not a substitute for human sign-off._"]
    return "\n".join(lines)
