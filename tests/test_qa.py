"""No database, no network — exercises the two pure parts of the epic-QA
role: the test-suite digest that gets put in front of the model, and the
JIRA comment rendering that is the report's only consumer.

`run_epic_qa` itself is covered through the graph tests (tests/test_graph.py)
with the model call faked; what's worth unit-testing here is the logic that
decides what the human ends up reading.
"""
from __future__ import annotations

from forge.agents.roles.qa import _test_suite_digest, format_qa_report_comment


def test_digest_reports_a_suite_that_could_not_run_as_not_run():
    """"Couldn't check" must not read as "passed" — a project with no
    profile supporting a full-suite run has to say so, or the report
    silently claims verification it never did."""
    digest = _test_suite_digest({"passed": True, "ran": False, "reason": "no_profile_supports_full_suite"})
    assert digest == {"ran": False, "reason": "no_profile_supports_full_suite"}


def test_digest_keeps_structured_failures_and_drops_stdout():
    digest = _test_suite_digest({
        "ran": True,
        "passed": False,
        "profiles": {
            "java_maven": {
                "passed": False, "tests_run": 42, "error_count": 2,
                "error_lines": ["com.example.FooTest#a: expected 1 but was 2"],
                "stdout": "x" * 100_000,
                "stderr": "noise",
            }
        },
    })
    profile = digest["profiles"]["java_maven"]
    assert digest["passed"] is False
    assert profile["failures"] == ["com.example.FooTest#a: expected 1 but was 2"]
    assert "stdout" not in profile
    # stderr is only carried when there are no structured failures to show.
    assert profile["stderr_tail"] == ""


def test_digest_falls_back_to_stderr_when_there_are_no_structured_failures():
    """A build failure produces no surefire XML at all — without this
    fallback the report would say "failed" and show nothing."""
    digest = _test_suite_digest({
        "ran": True, "passed": False,
        "profiles": {"java_maven": {"passed": False, "error_lines": [], "stderr": "COMPILATION ERROR"}},
    })
    assert "COMPILATION ERROR" in digest["profiles"]["java_maven"]["stderr_tail"]


def _report(**overrides) -> dict:
    base = {
        "verdict": "pass_with_findings",
        "summary": "Most of the flow is implemented.",
        "gaps": [],
        "scenarios": [],
        "regression_scope": [],
        "manual_checklist": [],
        "full_test_suite": {"ran": True, "passed": True, "profiles": {}},
    }
    base.update(overrides)
    return base


def test_comment_orders_gaps_by_severity():
    comment = format_qa_report_comment(
        _report(gaps=[
            {"severity": "minor", "story_key": "PROJ-3483", "acceptance_criterion": "AC3", "finding": "cosmetic"},
            {"severity": "blocker", "story_key": "PROJ-3484", "acceptance_criterion": "AC1", "finding": "missing"},
            {"severity": "major", "story_key": "PROJ-3485", "acceptance_criterion": "AC2", "finding": "partial"},
        ]),
        epic_key="PROJ-3480",
    )
    assert comment.index("BLOCKER") < comment.index("MAJOR") < comment.index("MINOR")


def test_comment_says_so_explicitly_when_there_are_no_gaps():
    """Silence is ambiguous — "none identified" tells the reader the check
    happened, an empty section doesn't."""
    assert "none identified" in format_qa_report_comment(_report(), epic_key="PROJ-3480")


def test_comment_surfaces_a_failing_suite_with_its_failures():
    comment = format_qa_report_comment(
        _report(verdict="fail", full_test_suite={
            "ran": True, "passed": False,
            "profiles": {"java_maven": {"passed": False, "tests_run": 42, "error_count": 1,
                                        "failures": ["com.example.FooTest#a: boom"]}},
        }),
        epic_key="PROJ-3480",
    )
    assert "FAILED on the merged branch" in comment
    assert "com.example.FooTest#a: boom" in comment
    assert "*Verdict:* FAIL" in comment


def test_comment_states_when_the_suite_did_not_run():
    comment = format_qa_report_comment(
        _report(full_test_suite={"ran": False, "reason": "no_profile_supports_full_suite"}),
        epic_key="PROJ-3480",
    )
    assert "not run" in comment
    assert "no_profile_supports_full_suite" in comment


def test_comment_never_claims_to_replace_human_signoff():
    assert "not a substitute for human sign-off" in format_qa_report_comment(_report(), epic_key="PROJ-3480")
