"""No database needed — exercises verifier.compare_to_plan's per-profile
dispatch/merge against real git (the `git_repo` fixture from conftest.py)
and controlled fake LanguageProfiles, so the dispatch logic itself is
tested independently of real Dart/Java tooling being installed."""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.agents.roles import verifier
from forge.tools import language_hooks


def _write(root: str, rel: str, content: str = "x\n") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _make_fake_profile(key: str, suffix: str, *, analyze_passes: bool = True, test_passes: bool = True, coverage_gap: bool = False):
    calls: list[tuple] = []

    def ensure_deps(worktree_path):
        calls.append(("ensure_dependencies", key))
        return {"passed": True}

    def analyze(worktree_path, targets):
        calls.append(("analyze", key, tuple(targets)))
        return {"passed": analyze_passes, "error_count": 0 if analyze_passes else 1, "error_lines": [] if analyze_passes else ["boom"]}

    def run_tests(worktree_path, targets):
        calls.append(("run_tests", key, tuple(targets)))
        if coverage_gap:
            return {"passed": False, "coverage_gap": True, "coverage_gap_detail": "lines 50% < 80%", "stdout": "", "stderr": ""}
        return {"passed": test_passes, "stdout": "", "stderr": "" if test_passes else "test failed"}

    profile = language_hooks.LanguageProfile(
        key=key,
        marker_files=(f"{key}.marker",),
        is_source=lambda p, s=suffix: p.endswith(s) and not p.endswith(f"_test{s}"),
        is_test=lambda p, s=suffix: p.endswith(f"_test{s}"),
        ensure_dependencies=ensure_deps,
        analyze=analyze,
        run_tests=run_tests,
    )
    return profile, calls


@pytest.fixture()
def registered_fake_profiles(monkeypatch):
    """Registers two independent fake profiles ('alpha'/'.alpha',
    'beta'/'.beta') into an isolated copy of the real registry — proves
    compare_to_plan's dispatch handles N profiles generically, without
    depending on real Dart/Java/Maven tooling."""
    language_hooks._ensure_default_profiles_registered()
    monkeypatch.setattr(language_hooks, "_PROFILES", dict(language_hooks._PROFILES))

    alpha_profile, alpha_calls = _make_fake_profile("alpha", ".alpha")
    beta_profile, beta_calls = _make_fake_profile("beta", ".beta")
    language_hooks.register_profile(alpha_profile)
    language_hooks.register_profile(beta_profile)
    return {"alpha": alpha_calls, "beta": beta_calls}


def test_compare_to_plan_dispatches_to_single_matching_profile(git_repo, registered_fake_profiles):
    _write(git_repo, "alpha.marker")
    _write(git_repo, "foo.alpha", "content\n")
    plan = {"files_to_create": [], "files_to_modify": [{"path": "foo.alpha"}]}
    file_changes = [{"path": "foo.alpha", "action": "modify"}]

    result = verifier.compare_to_plan(plan, git_repo, file_changes=file_changes)

    assert result["passed"] is True
    assert "alpha" in result["profiles"]
    assert "beta" not in result["profiles"]
    assert registered_fake_profiles["alpha"]  # ensure_dependencies/analyze were called
    assert registered_fake_profiles["beta"] == []


def test_compare_to_plan_mixed_repo_dispatches_both_profiles_to_own_files(git_repo, registered_fake_profiles):
    """The core claim of the redesign: a repo with two languages present
    gets both profiles invoked, each only seeing its own files."""
    _write(git_repo, "alpha.marker")
    _write(git_repo, "beta.marker")
    _write(git_repo, "foo.alpha", "content\n")
    _write(git_repo, "bar.beta", "content\n")
    plan = {"files_to_create": [], "files_to_modify": [{"path": "foo.alpha"}, {"path": "bar.beta"}]}
    file_changes = [{"path": "foo.alpha", "action": "modify"}, {"path": "bar.beta", "action": "modify"}]

    result = verifier.compare_to_plan(plan, git_repo, file_changes=file_changes)

    assert result["passed"] is True
    assert set(result["profiles"]) == {"alpha", "beta"}
    alpha_analyze_calls = [c for c in registered_fake_profiles["alpha"] if c[0] == "analyze"]
    beta_analyze_calls = [c for c in registered_fake_profiles["beta"] if c[0] == "analyze"]
    assert alpha_analyze_calls[0][2] == ("foo.alpha",)
    assert beta_analyze_calls[0][2] == ("bar.beta",)


def test_compare_to_plan_required_fix_from_failing_analyze(git_repo, registered_fake_profiles):
    _write(git_repo, "alpha.marker")
    _write(git_repo, "foo.alpha", "content\n")
    language_hooks._PROFILES["alpha"], _ = _make_fake_profile("alpha", ".alpha", analyze_passes=False)
    plan = {"files_to_create": [], "files_to_modify": [{"path": "foo.alpha"}]}
    file_changes = [{"path": "foo.alpha", "action": "modify"}]

    result = verifier.compare_to_plan(plan, git_repo, file_changes=file_changes)

    assert result["passed"] is False
    assert any("analyze failed" in fix for fix in result["required_fixes"])


def test_compare_to_plan_missing_planned_file_is_a_blocker(git_repo, registered_fake_profiles):
    plan = {"files_to_create": [{"path": "never_created.alpha"}], "files_to_modify": []}

    result = verifier.compare_to_plan(plan, git_repo)

    assert result["passed"] is False
    assert any("Missing planned new file" in fix for fix in result["required_fixes"])


def test_compare_to_plan_dev_target_missing_from_worktree_is_a_blocker(git_repo, registered_fake_profiles):
    """dev's file_changes claims foo.alpha was touched, but it isn't
    actually on disk (e.g. rolled back mid-loop) — must block, not silently
    skip verification for that file."""
    _write(git_repo, "alpha.marker")
    plan = {"files_to_create": [], "files_to_modify": [{"path": "foo.alpha"}]}
    file_changes = [{"path": "foo.alpha", "action": "modify"}]

    result = verifier.compare_to_plan(plan, git_repo, file_changes=file_changes)

    assert result["passed"] is False
    assert any("Dev target missing in worktree" in fix for fix in result["required_fixes"])


def test_compare_to_plan_coverage_gap_does_not_block_but_is_reported(git_repo, registered_fake_profiles):
    """A coverage-threshold miss (tests themselves passed) must not join
    required_fixes — that would send a story through pointless extra dev
    iterations trying to "fix" a repo-wide coverage bar it can't move — but
    the gap still has to surface so the daemon can flag the story."""
    _write(git_repo, "alpha.marker")
    _write(git_repo, "foo_test.alpha", "content\n")
    language_hooks._PROFILES["alpha"], _ = _make_fake_profile("alpha", ".alpha", coverage_gap=True)
    plan = {"files_to_create": [], "files_to_modify": [{"path": "foo_test.alpha"}]}
    file_changes = [{"path": "foo_test.alpha", "action": "modify"}]

    result = verifier.compare_to_plan(plan, git_repo, file_changes=file_changes)

    assert result["passed"] is True
    assert result["required_fixes"] == []
    assert result["coverage_gap"] is True
    assert result["coverage_gap_detail"] == "alpha: lines 50% < 80%"


def test_compare_to_plan_flags_planned_file_dev_never_touched(git_repo, registered_fake_profiles):
    """Regression: a planned files_to_modify entry used to only get checked
    when dev's own file_changes claimed it — an empty (or inaccurate)
    file_changes list made this whole loop skip every planned file instead
    of flagging them as untouched, so a dev run that changed nothing at all
    (e.g. forge/agents/roles/dev_loop.py's agent loop silently producing no
    messages) vacuously passed verification. `foo.alpha` here
    is committed and unmodified, and file_changes is empty — the real git
    diff is what must catch this, not dev's self-report."""
    _write(git_repo, "alpha.marker")
    _write(git_repo, "foo.alpha", "content\n")
    subprocess.run(["git", "-C", git_repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", git_repo, "commit", "-m", "seed foo.alpha"], check=True)
    plan = {"files_to_create": [], "files_to_modify": [{"path": "foo.alpha"}]}

    result = verifier.compare_to_plan(plan, git_repo, file_changes=[])

    assert result["passed"] is False
    assert any("Dev did not modify target file: foo.alpha" in fix for fix in result["required_fixes"])


def test_compare_to_plan_no_recognized_language_is_a_noop_pass(git_repo, registered_fake_profiles):
    _write(git_repo, "foo.txt", "content\n")
    plan = {"files_to_create": [], "files_to_modify": [{"path": "foo.txt"}]}
    file_changes = [{"path": "foo.txt", "action": "modify"}]

    result = verifier.compare_to_plan(plan, git_repo, file_changes=file_changes)

    assert result["passed"] is True
    assert result["profiles"] == {}


# --- Tier 1 fast-fail feedback -------------------------------------------


def test_format_gate_feedback_leads_with_a_count_and_the_files_then_verbatim_errors():
    gate = {
        "passed": False,
        "issues": [
            {"severity": "blocker", "file": None, "reason": "dart_flutter: analyze failed",
             "details": "lib/auth/auth_service.dart:12:5: error: Undefined name 'foo'.\nlib/auth/auth_service.dart:30:1: error: Expected ';'."},
            {"severity": "blocker", "file": "lib/auth/login_page.dart", "reason": "Missing planned new file: lib/auth/login_page.dart"},
        ],
        "required_fixes": ["dart_flutter: analyze failed", "Missing planned new file: lib/auth/login_page.dart"],
    }

    text = verifier.format_gate_feedback(gate, attempt=1, max_attempts=3)

    first_line = text.splitlines()[0]
    assert first_line.startswith("Deterministic checks FAILED with 2 problem(s) in lib/auth/auth_service.dart, lib/auth/login_page.dart")
    assert "(fix attempt 1/3)" in first_line
    assert "1. dart_flutter: analyze failed" in text
    assert "   lib/auth/auth_service.dart:12:5: error: Undefined name 'foo'." in text
    assert "2. Missing planned new file: lib/auth/login_page.dart" in text
    assert "analyze_changed_files_tool" in text
    assert "do not rewrite files from scratch" in text


def test_format_gate_feedback_falls_back_to_the_profile_analyze_error_lines():
    gate = {
        "passed": False,
        "issues": [{"severity": "blocker", "file": None, "reason": "alpha: analyze failed"}],
        "required_fixes": ["alpha: analyze failed"],
        "profiles": {"alpha": {"analyze": {"passed": False, "error_lines": ["foo.alpha:1:1: boom"]}}},
    }

    text = verifier.format_gate_feedback(gate, attempt=2, max_attempts=3)

    assert "in foo.alpha" in text.splitlines()[0]
    assert "   foo.alpha:1:1: boom" in text


def test_format_gate_feedback_truncates_long_test_output_to_its_tail():
    gate = {"passed": False, "issues": [{"reason": "beta: tests failed", "details": "noise\n" * 2000 + "FAILED test_x — assertion"}]}

    text = verifier.format_gate_feedback(gate, attempt=1, max_attempts=3)

    assert "FAILED test_x — assertion" in text
    assert len(text) < 3_000


async def test_run_verifier_reuses_a_precomputed_gate_instead_of_rerunning_the_toolchain(git_repo, registered_fake_profiles, monkeypatch):
    """gate_node already ran compare_to_plan on this exact worktree —
    run_verifier must not run the (slow) toolchain a second time."""
    calls = registered_fake_profiles["alpha"]
    _write(git_repo, "alpha.marker")
    _write(git_repo, "foo.alpha", "content\n")
    monkeypatch.setattr(verifier, "compare_to_plan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("toolchain re-run")))
    # Skip the LLM review entirely via the trivial-diff threshold — no
    # provider, and no DB either (this file stays DB-free).
    monkeypatch.setattr(verifier, "get_int_config", lambda key, default=0: 10**9)

    gate = {"passed": True, "issues": [], "required_fixes": [], "profiles": {}, "changed_files": ["foo.alpha"], "dev_targets": ["foo.alpha"], "coverage_gap": False}
    result = await verifier.run_verifier(
        plan={"files_to_modify": [{"path": "foo.alpha"}]}, acceptance_criteria=[], worktree_path=git_repo,
        file_changes=[{"path": "foo.alpha"}], precomputed_gate=gate,
    )

    assert result["verifier_report"]["passed"] is True
    assert result["verifier_report"]["deterministic_gate"] is gate
    assert not [c for c in calls if c[0] == "analyze"]


async def test_run_verifier_refuses_to_pass_a_run_with_nothing_to_verify(git_repo, registered_fake_profiles, monkeypatch):
    """The vacuous pass, end to end. An empty plan makes compare_to_plan
    check nothing and report passed, and the review is then asked to reject
    only a "planned acceptance criterion clearly unmet" — of which an empty
    plan has none. Both halves agree on a story that produced no work. The
    reviewer is never asked: the LLM call here would raise, and the test
    passes only because the guard returns before it."""
    monkeypatch.setattr(verifier, "get_int_config", lambda key, default=0: 0)  # no trivial-diff skip
    gate = {"passed": True, "issues": [], "required_fixes": [], "profiles": {}, "changed_files": [], "dev_targets": []}

    result = await verifier.run_verifier(
        plan={"feature_summary": "Show the currency", "files_to_create": [], "files_to_modify": []},
        acceptance_criteria=[], worktree_path=git_repo, file_changes=[], precomputed_gate=gate,
    )

    report = result["verifier_report"]
    assert report["passed"] is False
    assert report["nothing_to_verify"] is True
    assert "Nothing to verify" in report["required_fixes"][0]


async def test_a_declared_already_satisfied_file_still_reaches_the_reviewer(git_repo, registered_fake_profiles, monkeypatch):
    """A run whose only outcome is "this file already does the job" has an
    empty diff too, but that claim is exactly what the review exists to
    judge — the guard must not swallow it."""
    reviewed = {}

    async def fake_review(**kwargs):
        reviewed["called"] = True
        return {"passed": True, "issues": [], "required_fixes": []}, {"model": "fake"}

    monkeypatch.setattr(verifier, "get_int_config", lambda key, default=0: 0)
    monkeypatch.setattr(verifier, "chat_completion_json", fake_review)
    gate = {
        "passed": True, "issues": [], "required_fixes": [], "profiles": {}, "changed_files": [], "dev_targets": [],
        "assumed_satisfied": [{"path": "foo.alpha", "reason": "already formats the currency"}],
    }

    result = await verifier.run_verifier(
        plan={"files_to_create": [], "files_to_modify": []}, acceptance_criteria=[],
        worktree_path=git_repo, file_changes=[], precomputed_gate=gate,
    )

    assert reviewed.get("called") is True
    assert result["verifier_report"]["passed"] is True
    assert "nothing_to_verify" not in result["verifier_report"]


# --- plan-coverage feedback: tell dev to WRITE, not to investigate -------


def _coverage_issue(kind: str, path: str) -> dict:
    reasons = {
        verifier.KIND_MISSING_NEW_FILE: f"Missing planned new file: {path}",
        verifier.KIND_UNMODIFIED_TARGET: f"Dev did not modify target file: {path}",
        verifier.KIND_MISSING_TARGET: f"Dev target missing in worktree: {path}",
    }
    return {"severity": "blocker", "file": path, "reason": reasons[kind], "kind": kind}


def _gate_of(*issues: dict) -> dict:
    return {"passed": False, "issues": list(issues), "required_fixes": [i["reason"] for i in issues]}


def test_compare_to_plan_tags_each_plan_coverage_blocker_with_what_it_is(git_repo):
    plan = {"files_to_create": [{"path": "new_file.py"}], "files_to_modify": [{"path": "README.md"}, {"path": "nowhere.py"}]}

    issues = {i["file"]: i["kind"] for i in verifier.compare_to_plan(plan, git_repo)["issues"]}

    assert issues == {
        "new_file.py": verifier.KIND_MISSING_NEW_FILE,
        "README.md": verifier.KIND_UNMODIFIED_TARGET,
        "nowhere.py": verifier.KIND_MISSING_TARGET,
    }


def test_the_kind_survives_the_per_repo_merge_with_the_repo_prefix_dev_needs():
    from forge.tools.workspace import Workspace

    workspace = Workspace(default_label="ui", roots={"ui": "/w/ui", "api": "/w/api"})
    reports = {
        "ui": {"issues": [_coverage_issue(verifier.KIND_MISSING_NEW_FILE, "src/A.tsx")], "required_fixes": ["x"]},
        "api": {"issues": [_coverage_issue(verifier.KIND_UNMODIFIED_TARGET, "src/B.java")], "required_fixes": ["y"]},
    }

    merged = {i["kind"]: i["file"] for i in verifier._merge_gate_reports(reports, workspace)["issues"]}

    assert merged == {verifier.KIND_MISSING_NEW_FILE: "src/A.tsx", verifier.KIND_UNMODIFIED_TARGET: "@api/src/B.java"}


def test_feedback_for_a_run_that_wrote_nothing_lists_the_files_by_the_tool_to_use_and_says_write_now():
    """The shape of the run that failed three rounds: one planned new file
    and 25 planned modifications, none touched."""
    gate = _gate_of(
        _coverage_issue(verifier.KIND_MISSING_NEW_FILE, "pricing-web/scripts/generate.js"),
        *(_coverage_issue(verifier.KIND_UNMODIFIED_TARGET, f"pricing-api/src/File{i}.java") for i in range(25)),
    )

    text = verifier.format_gate_feedback(gate, attempt=1, max_attempts=3, wrote_nothing=True)

    assert text.splitlines()[0].startswith("Deterministic checks FAILED with 26 problem(s)")
    assert "Your last attempt wrote NO files" in text
    assert "This is your last attempt" not in text  # attempt 1 of 3
    assert "PLANNED FILES STILL NOT WRITTEN (26):" in text
    assert "Create these with write_file_tool:\n    - pricing-web/scripts/generate.js" in text
    assert "Change these with edit_file_tool / apply_edits_tool:\n    - pricing-api/src/File0.java" in text
    assert "pricing-api/src/File19.java" in text and "pricing-api/src/File20.java" not in text
    assert "... and 5 more" in text
    # the 26 near-identical lines are not also repeated as a numbered list
    assert "1. Dev did not modify target file" not in text
    assert "Your next turn must be write_file_tool / edit_file_tool / apply_edits_tool" in text
    assert "ONE turn" in text and "Do not explore or re-read first" in text
    # the advice that fits an analyzer error is wrong for a file that does not exist
    assert "do not rewrite files from scratch" not in text


def test_the_last_attempt_says_the_story_fails_if_it_ends_the_same_way():
    gate = _gate_of(_coverage_issue(verifier.KIND_UNMODIFIED_TARGET, "a.py"))

    assert "This is your last attempt" in verifier.format_gate_feedback(gate, attempt=2, max_attempts=3, wrote_nothing=True)
    assert "This is your last attempt" not in verifier.format_gate_feedback(gate, attempt=1, max_attempts=3, wrote_nothing=True)


def test_feedback_does_not_claim_nothing_was_written_when_something_was():
    gate = _gate_of(_coverage_issue(verifier.KIND_UNMODIFIED_TARGET, "b.py"))

    text = verifier.format_gate_feedback(gate, attempt=1, max_attempts=3, wrote_nothing=False)

    assert "wrote NO files" not in text
    assert "PLANNED FILES STILL NOT WRITTEN (1):" in text and "Your next turn must be write_file_tool" in text


def test_a_path_that_is_not_in_the_worktree_is_told_to_check_the_path_first():
    gate = _gate_of(_coverage_issue(verifier.KIND_MISSING_TARGET, "src/Typo.java"))

    text = verifier.format_gate_feedback(gate, attempt=1, max_attempts=3)

    assert "not in the worktree — check the path in the plan" in text and "src/Typo.java" in text


def test_analyzer_failures_alongside_unwritten_files_are_kept_and_numbered_from_one():
    gate = _gate_of(
        _coverage_issue(verifier.KIND_MISSING_NEW_FILE, "new.py"),
        {"severity": "blocker", "file": None, "reason": "alpha: analyze failed", "details": "foo.alpha:1:1: boom"},
    )

    text = verifier.format_gate_feedback(gate, attempt=1, max_attempts=3)

    assert "PLANNED FILES STILL NOT WRITTEN (1):" in text
    assert "also reported these — fix them as well:" in text
    assert "1. alpha: analyze failed" in text and "   foo.alpha:1:1: boom" in text
    assert "Your next turn must be write_file_tool" in text


def test_feedback_with_no_unwritten_files_is_the_fix_these_failures_message_it_always_was():
    gate = _gate_of({"severity": "blocker", "file": None, "reason": "alpha: analyze failed", "details": "foo.alpha:1:1: boom"})

    text = verifier.format_gate_feedback(gate, attempt=1, max_attempts=3, wrote_nothing=False)

    assert "Fix these specific failures — nothing else:" in text
    assert "do not rewrite files from scratch" in text
    assert "PLANNED FILES STILL NOT WRITTEN" not in text and "wrote NO files" not in text
