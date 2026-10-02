"""Pluggable per-language tooling: dependency install, analyze/compile
checks, test running, and the deterministic verifier gate, all behind one
`LanguageProfile` registry. A worktree can match more than one profile (a
repo with both `pom.xml` and `vite.config.ts` at its root) — every entry
point here operates over "every detected profile", not a single language,
so a mixed-stack repo is handled by construction rather than a special
case bolted on afterward.

Adding a language means writing one `LanguageProfile` (see
`dart_tools.dart_flutter_profile()` / `java_tools.java_maven_profile()` /
`vue_tools.vue_vite_profile()` for examples) and registering it in
`_ensure_default_profiles_registered` below — nothing else in this file,
or in any caller, needs to change.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from forge.tools.agent_env import agent_environment


@dataclass
class VerifyContext:
    """Everything a profile's `verify` needs to run the deterministic gate
    for its slice of a worktree."""

    worktree_path: str
    plan: dict[str, Any]
    changed: set[str]
    dev_targets: set[str]
    analyze_targets: list[str]
    test_targets: list[str]
    run_id: str | None = None
    prior_state: Any = None


@dataclass(frozen=True)
class LanguageProfile:
    key: str
    marker_files: tuple[str, ...]
    is_source: Callable[[str], bool]
    is_test: Callable[[str], bool]
    ensure_dependencies: Callable[[str], dict]
    analyze: Callable[[str, list[str]], dict]
    run_tests: Callable[[str, list[str]], dict]
    # Runs the project's ENTIRE test suite, unscoped. Distinct from
    # run_tests(worktree, []) because the two mean opposite things to
    # different toolchains — `flutter test` with no arguments runs
    # everything, whereas `mvn test -Dtest=` with no classes is a no-op, so
    # "no targets" can't safely be overloaded to mean "all targets".
    # Used only by the epic-level QA pass (forge/agents/roles/qa.py), which
    # is the first point at which every story's changes coexist on one
    # branch and a whole-suite run is meaningful. None -> unsupported.
    run_all_tests: Callable[[str], dict] | None = None
    # Full deterministic gate. None -> the shared default (ensure_dependencies
    # -> analyze -> run_tests) is used; a profile only needs its own
    # implementation when its pipeline has extra steps (Dart's build_runner
    # + DI-registration check between pub_get and analyze).
    verify: Callable[[VerifyContext], dict] | None = None
    # Optional per-modified-file semantic check (e.g. Dart's "did you
    # silently remove a public method" check). (rel_path, original_content,
    # current_content) -> issue text, or None if nothing to flag.
    check_modified_file: Callable[[str, str, str], str | None] | None = None
    # Runs once after the dev loop finishes editing, over the files it
    # actually changed that this profile claims — for languages with a
    # codegen step (Dart's build_runner). Returns {passed, skipped, state}.
    after_dev_changes: Callable[[str, list[str]], dict] | None = None
    safe_command_prefixes: tuple[tuple[str, ...], ...] = ()
    # Command prefixes (tokenized) that are this language's analyze/compile
    # check — forge/tools/dev_tools.py's run_command_tool redirects any of
    # these to analyze_changed_files_tool instead of running them directly.
    analyze_command_prefixes: tuple[tuple[str, ...], ...] = ()
    # Command prefixes that run this language's tests.
    test_command_prefixes: tuple[tuple[str, ...], ...] = ()
    # (worktree_path, matched_prefix, requested_args_after_prefix, planned_test_targets)
    # -> (error_message, final_command_tokens). error_message set means
    # BLOCKED (final_command_tokens is then ignored); final_command_tokens
    # set means "run this instead"; both None means "run the original
    # command as-is".
    format_test_command: Callable[[str, tuple[str, ...], list[str], list[str]], tuple[str | None, list[str] | None]] | None = None
    # Markdown blocks concatenated into each role's system prompt, one per
    # detected profile. Split by role because a planner-time rule (e.g.
    # "the DI registration file must be in files_to_modify") isn't
    # something the dev loop's own coding rules (e.g. constructor
    # patterns) belong mixed in with, and vice versa.
    planner_conventions: str = ""
    dev_conventions: str = ""
    # Appended to the verifier's LLM-review system prompt — for guidance
    # specific to how a reviewer should read this language's diffs (e.g.
    # Dart's "generated DI files are gitignored, don't flag them as
    # missing" note).
    verifier_review_conventions: str = ""
    # Extra environment for every command run in a worktree this profile
    # matches — the build check's and the developer step's own (`run` below,
    # forge/tools/safe_shell.py). Java's picks the JDK the project states
    # (forge/tools/jdk.py). (worktree_path) -> {name: value}.
    command_env: Callable[[str], dict[str, str]] | None = None
    # Folders this toolchain writes its output to (gitignore patterns).
    # Never staged or committed, even where the repo does not ignore them
    # (forge/tools/worktree.py's exclude_build_output).
    build_output: tuple[str, ...] = ()
    # (worktree_path, failed step's result) -> why the failure is the build
    # environment rather than the code, or None. Such a failure stops the
    # run instead of going back to the developer step, which cannot fix a
    # toolchain and will "fix" working code trying (MKT2-866).
    environment_problem: Callable[[str, dict], str | None] | None = None


_PROFILES: dict[str, LanguageProfile] = {}
_DEFAULTS_REGISTERED = False


def register_profile(profile: LanguageProfile) -> None:
    _PROFILES[profile.key] = profile


def _ensure_default_profiles_registered() -> None:
    """Lazily imports and registers the built-in profiles on first use.
    Lazy on purpose: dart_tools.py/java_tools.py/vue_tools.py import
    LanguageProfile from this module, so importing them back at module
    load time here would be circular — importing them from inside a
    function instead, the first time anything actually needs a profile,
    sidesteps that."""
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    _DEFAULTS_REGISTERED = True

    from forge.tools import dart_tools, java_tools, vue_tools

    register_profile(dart_tools.dart_flutter_profile())
    register_profile(java_tools.java_maven_profile())
    register_profile(java_tools.java_gradle_profile())
    register_profile(vue_tools.vue_vite_profile())


def all_profiles() -> list[LanguageProfile]:
    _ensure_default_profiles_registered()
    return list(_PROFILES.values())


def detect_profiles(worktree_path: str) -> list[LanguageProfile]:
    _ensure_default_profiles_registered()
    return [
        profile
        for profile in _PROFILES.values()
        if any(os.path.isfile(os.path.join(worktree_path, marker)) for marker in profile.marker_files)
    ]


def detect_language(worktree_path: str) -> str | None:
    """Back-compat single-language accessor for callers that only need a
    label (logging, etc). Prefer detect_profiles for anything that acts on
    the result — a worktree may match more than one profile."""
    profiles = detect_profiles(worktree_path)
    return profiles[0].key if profiles else None


def classify_targets_by_profile(worktree_path: str, paths: set[str] | list[str]) -> dict[str, tuple[list[str], list[str]]]:
    """Per-profile (analyze_targets, test_targets) for the given paths. A
    path matching no detected profile is dropped (not force-included), so
    an unrecognized file type in a mixed repo never becomes a compile
    target for the wrong tool."""
    profiles = detect_profiles(worktree_path)
    buckets: dict[str, tuple[list[str], list[str]]] = {p.key: ([], []) for p in profiles}
    for path in paths:
        for profile in profiles:
            if profile.is_test(path):
                buckets[profile.key][1].append(path)
                break
            if profile.is_source(path):
                buckets[profile.key][0].append(path)
                break
    return {key: (sorted(a), sorted(t)) for key, (a, t) in buckets.items()}


def classify_targets(worktree_path: str, paths: set[str] | list[str]) -> tuple[list[str], list[str]]:
    """Flattened across every detected profile — for callers (the dev
    loop's tool scoping) that just need "is this analyze-worthy / is this
    a test file", not which language it belongs to."""
    by_profile = classify_targets_by_profile(worktree_path, paths)
    analyze = sorted({p for a, _ in by_profile.values() for p in a})
    test = sorted({p for _, t in by_profile.values() for p in t})
    return analyze, test


def profile_for_path(worktree_path: str, path: str) -> LanguageProfile | None:
    for profile in detect_profiles(worktree_path):
        if profile.is_source(path) or profile.is_test(path):
            return profile
    return None


def ensure_dependencies(worktree_path: str) -> dict:
    """Run every detected profile's dependency-install step once a
    worktree is created."""
    profiles = detect_profiles(worktree_path)
    if not profiles:
        return {"passed": True, "skipped": True, "reason": "no_recognized_project_file"}
    if len(profiles) == 1:
        return profiles[0].ensure_dependencies(worktree_path)
    results = {p.key: p.ensure_dependencies(worktree_path) for p in profiles}
    return {"passed": all(r.get("passed", True) for r in results.values()), "profiles": results}


def run_analyze(worktree_path: str, targets: list[str] | None = None) -> dict:
    """Compile/lint check dispatcher — the generic entry point
    forge/tools/dev_tools.py's analyze_changed_files_tool calls mid-dev-loop.
    Splits targets across whichever profile(s) claim them and merges
    error_lines/error_count so a mixed-repo change gets one combined
    report."""
    profiles = detect_profiles(worktree_path)
    if not profiles:
        return {"passed": True, "skipped": True, "reason": "no_recognized_project_file"}

    targets = targets or []
    by_key: dict[str, list[str]] = {}
    for target in targets:
        for profile in profiles:
            if profile.is_source(target):
                by_key.setdefault(profile.key, []).append(target)
                break

    if not by_key:
        return {"passed": True, "skipped": True, "reason": "no_targets_matched_a_profile"}

    if len(by_key) == 1:
        ((key, its_targets),) = by_key.items()
        return _PROFILES[key].analyze(worktree_path, its_targets)

    error_lines: list[str] = []
    error_count = 0
    passed = True
    sub: dict[str, dict] = {}
    for key, its_targets in by_key.items():
        result = _PROFILES[key].analyze(worktree_path, its_targets)
        sub[key] = result
        passed = passed and result.get("passed", True)
        error_count += result.get("error_count", 0)
        error_lines.extend(result.get("error_lines", []))
    return {"passed": passed, "error_count": error_count, "error_lines": error_lines[:25], "profiles": sub}


def run_full_test_suites(worktree_path: str) -> dict:
    """Runs every detected profile's complete test suite and merges the
    results. This is the epic-QA counterpart to `run_analyze` — the
    per-story verifier deliberately runs only the tests a story's own plan
    named, so a test broken by the *combination* of two stories is invisible
    until something runs the whole suite over the merged branch. Nothing
    else in the pipeline does that.

    Returns {passed, ran, profiles} — `ran` is False when no detected
    profile supports a full-suite run, which is a "couldn't check", not a
    pass, and is reported as such rather than being silently swallowed."""
    profiles = [p for p in detect_profiles(worktree_path) if p.run_all_tests is not None]
    if not profiles:
        return {"passed": True, "ran": False, "reason": "no_profile_supports_full_suite", "profiles": {}}

    results = {p.key: p.run_all_tests(worktree_path) for p in profiles}
    return {
        "passed": all(r.get("passed", True) for r in results.values()),
        "ran": True,
        "profiles": results,
    }


def run_profile_verify(profile: LanguageProfile, ctx: VerifyContext) -> dict:
    if profile.verify is not None:
        return profile.verify(ctx)
    return _default_verify(profile, ctx)


def _mark_environment(profile: LanguageProfile, worktree_path: str, result: dict, issue: dict) -> dict:
    """`issue`, flagged `environment` with the reason when the profile says
    the failure is the build environment (see LanguageProfile.
    environment_problem) — forge/orchestration/graph.py's gate_node stops
    the run on it."""
    reason = profile.environment_problem(worktree_path, result) if profile.environment_problem else None
    return {**issue, "environment": reason} if reason else issue


def _default_verify(profile: LanguageProfile, ctx: VerifyContext) -> dict:
    """Shared verify pipeline for any language that doesn't need more than
    ensure_dependencies -> analyze -> run_tests (e.g. Java: Spring wiring
    is annotation/component-scan based, so a broken registration already
    surfaces as a compile error or a failing context-load test — no
    separate DI-check or codegen step like Dart's is needed)."""
    issues: list[dict[str, Any]] = []
    required_fixes: list[str] = []

    deps: dict[str, Any] = {"passed": True, "skipped": True}
    if ctx.analyze_targets or ctx.test_targets:
        deps = profile.ensure_dependencies(ctx.worktree_path)
        if not deps.get("passed", True):
            msg = f"{profile.key}: dependency install failed"
            issue = {"severity": "blocker", "file": None, "reason": msg, "details": deps.get("stdout") or deps.get("stderr")}
            issues.append(_mark_environment(profile, ctx.worktree_path, deps, issue))
            required_fixes.append(msg)

    analyze: dict[str, Any] = {"passed": True, "skipped": True, "targets": ctx.analyze_targets}
    if ctx.analyze_targets and deps.get("passed", True):
        analyze = profile.analyze(ctx.worktree_path, ctx.analyze_targets)
        analyze["targets"] = ctx.analyze_targets
        if not analyze.get("passed", True):
            msg = f"{profile.key}: analyze failed"
            details = "\n".join(analyze.get("error_lines") or []) or analyze.get("stdout") or analyze.get("stderr")
            issue = {"severity": "blocker", "file": None, "reason": msg, "details": details}
            issues.append(_mark_environment(profile, ctx.worktree_path, analyze, issue))
            required_fixes.append(msg)

    test_results: dict[str, Any] = {"passed": True, "skipped": True}
    coverage_gap = False
    coverage_gap_detail: str | None = None
    if ctx.test_targets and deps.get("passed", True) and analyze.get("passed", True):
        test_results = profile.run_tests(ctx.worktree_path, ctx.test_targets)
        if test_results.get("coverage_gap"):
            # A coverage-threshold miss, not an implementation failure —
            # the tests themselves passed, so this does not block (does not
            # join required_fixes); it's surfaced separately so the daemon
            # can flag the story instead of silently dropping the signal.
            coverage_gap = True
            coverage_gap_detail = test_results.get("coverage_gap_detail")
        elif not test_results.get("passed", True):
            msg = f"{profile.key}: tests failed"
            details = (test_results.get("stdout", "") + "\n" + test_results.get("stderr", ""))[-4000:]
            issues.append({"severity": "blocker", "file": None, "reason": msg, "details": details})
            required_fixes.append(msg)

    return {
        "passed": len(required_fixes) == 0,
        "issues": issues,
        "required_fixes": required_fixes,
        "coverage_gap": coverage_gap,
        "coverage_gap_detail": coverage_gap_detail,
        "report": {"dependencies": deps, "analyze": analyze, "test_results": test_results},
        "state": None,
    }


def profile_command_env(worktree_path: str) -> dict[str, str]:
    """The environment commands in `worktree_path` run with: the process's own
    cut down to what a toolchain needs (forge/tools/agent_env.py — no
    credentials, no `DATABASE_URL`), plus every matching profile's
    `command_env` on top."""
    extra: dict[str, str] = {}
    for profile in detect_profiles(worktree_path):
        if profile.command_env is not None:
            extra.update(profile.command_env(worktree_path))
    return agent_environment(extra=extra)


def build_output_patterns(worktree_path: str) -> list[str]:
    """Every matching profile's build-output folders, in order."""
    patterns: list[str] = []
    for profile in detect_profiles(worktree_path):
        patterns.extend(p for p in profile.build_output if p not in patterns)
    return patterns


def run(cmd: list[str], *, cwd: str, timeout: int, input: str | None = None) -> dict:
    """Shared subprocess runner for per-language hooks (dart_tools.py,
    java_tools.py, vue_tools.py) — consistent timeout/output-capping
    behavior, and the environment the worktree's profiles ask for
    (`profile_command_env`).
    `input` is sent on stdin."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout, env=profile_command_env(cwd),
            input=input,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "passed": False,
            "timed_out": True,
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": f"{cmd[0]} timed out after {timeout}s",
            "command": " ".join(cmd),
        }
    return {
        "passed": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:] if proc.stdout else "",
        "stderr": proc.stderr[-8000:] if proc.stderr else "",
        "timed_out": False,
        "command": " ".join(cmd),
    }

