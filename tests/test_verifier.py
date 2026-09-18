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
