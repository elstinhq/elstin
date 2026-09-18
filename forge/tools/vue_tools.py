"""Vue 3 + Vite `vue-tsc` / Vitest invocation, and the `LanguageProfile`
that wires it into forge/tools/language_hooks.py's registry.

Modeled on apps like mkt-adminportal: Vite at the root, TypeScript via
`vue-tsc`, unit tests via Vitest, npm as the package manager. Uses the
shared default verify pipeline (ensure_dependencies -> analyze ->
run_tests) — no codegen/DI step between install and typecheck.
"""
from __future__ import annotations

import json
import os
import re

from forge.tools import language_hooks
from forge.tools.language_hooks import LanguageProfile

NPM_DEPENDENCY_TIMEOUT_SECONDS = 300
VUE_TSC_TIMEOUT_SECONDS = 180
ESLINT_TIMEOUT_SECONDS = 180
VITEST_TIMEOUT_SECONDS = 600

_SOURCE_EXTENSIONS = (".vue", ".ts", ".tsx", ".js", ".jsx")
_TEST_SUFFIXES = (
    ".spec.ts",
    ".spec.tsx",
    ".spec.js",
    ".spec.jsx",
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
)

# Vitest's own summary lines and coverage-threshold errors — used to tell
# "every test passed, only the coverage bar was missed" apart from a real
# test failure (see _coverage_only_failure below).
_VITEST_TESTS_FAILED_RE = re.compile(r"\b(?:Test Files|Tests)\s+\d+\s+failed", re.IGNORECASE)
_VITEST_TESTS_PASSED_RE = re.compile(r"Test Files\s+\d+\s+passed", re.IGNORECASE)
_VITEST_COVERAGE_THRESHOLD_RE = re.compile(
    r"^ERROR: Coverage for (\S+) \(([\d.]+)%\) does not meet .*? threshold \((\d+)%\)", re.MULTILINE
)

# TypeScript / vue-tsc: path/to/file.ts(12,34): error TS2304: ...
_TSC_ERROR_RE = re.compile(
    r"^(.+?)\((\d+),(\d+)\):\s+error\s+TS\d+:\s*(.*)$"
)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _is_under(path: str, prefix: str) -> bool:
    normalized = _norm(path)
    return normalized == prefix or normalized.startswith(prefix + "/")


def _has_test_suffix(path: str) -> bool:
    normalized = _norm(path).lower()
    return any(normalized.endswith(suffix) for suffix in _TEST_SUFFIXES)


def is_vue_test(path: str) -> bool:
    """Vitest-style unit tests: `*.spec.*` / `*.test.*`, or anything under
    `tests/` / `__tests__/` with a JS/TS/Vue extension."""
    normalized = _norm(path)
    if "node_modules/" in f"/{normalized}/":
        return False
    if _has_test_suffix(normalized):
        return True
    if _is_under(normalized, "tests") or "/__tests__/" in f"/{normalized}/":
        return any(normalized.endswith(ext) for ext in _SOURCE_EXTENSIONS)
    return False


def is_vue_source(path: str) -> bool:
    """App source under `src/` (or a few common root layout dirs), excluding
    tests and node_modules. Spec/test files are never source."""
    if is_vue_test(path):
        return False
    normalized = _norm(path)
    if "node_modules/" in f"/{normalized}/":
        return False
    if not any(normalized.endswith(ext) for ext in _SOURCE_EXTENSIONS):
        return False
    return (
        _is_under(normalized, "src")
        or _is_under(normalized, "components")
        or _is_under(normalized, "pages")
        or _is_under(normalized, "composables")
        or _is_under(normalized, "stores")
    )


def _package_scripts(worktree_path: str) -> dict[str, str]:
    package_json = os.path.join(worktree_path, "package.json")
    try:
        with open(package_json, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _has_tsconfig(worktree_path: str) -> bool:
    return any(
        os.path.isfile(os.path.join(worktree_path, name))
        for name in ("tsconfig.json", "tsconfig.app.json", "tsconfig.node.json")
    )


def ensure_npm_dependencies(worktree_path: str) -> dict:
    """Prefer `npm ci` when a lockfile is present — reproducible installs
    matching CI; fall back to `npm install` for repos without one."""
    lockfile = os.path.join(worktree_path, "package-lock.json")
    cmd = ["npm", "ci"] if os.path.isfile(lockfile) else ["npm", "install"]
    return language_hooks.run(cmd, cwd=worktree_path, timeout=NPM_DEPENDENCY_TIMEOUT_SECONDS)


def parse_tsc_errors(output: str, worktree_path: str) -> tuple[int, list[str]]:
    errors: list[str] = []
    for line in output.splitlines():
        match = _TSC_ERROR_RE.match(line.strip())
        if not match:
            continue
        abs_or_rel, lineno, col, message = match.groups()
        path = abs_or_rel
        if os.path.isabs(path):
            try:
                path = os.path.relpath(path, worktree_path)
            except ValueError:
                pass
        errors.append(f"{_norm(path)}:{lineno}:{col}: {message}".strip())
    return len(errors), errors[:25]


def run_vue_analyze(worktree_path: str, targets: list[str]) -> dict:
    """Typecheck with `vue-tsc` when a TS project is present; otherwise
    fall back to `npm run lint` if that script exists. `targets` are
    unused — both tools are project-wide — but kept for LanguageProfile
    API compatibility with scoped analyze dispatch."""
    del targets  # project-wide checkers; path list is for dispatch only
    if _has_tsconfig(worktree_path):
        result = language_hooks.run(
            ["npx", "vue-tsc", "-b", "--noEmit"],
            cwd=worktree_path,
            timeout=VUE_TSC_TIMEOUT_SECONDS,
        )
        combined = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part)
        error_count, error_lines = parse_tsc_errors(combined, worktree_path)
        return {
            **result,
            "passed": result.get("passed", False) and error_count == 0,
            "error_count": error_count,
            "error_lines": error_lines,
        }

    if "lint" in _package_scripts(worktree_path):
        return language_hooks.run(
            ["npm", "run", "lint"],
            cwd=worktree_path,
            timeout=ESLINT_TIMEOUT_SECONDS,
        )

    return {
        "passed": True,
        "skipped": True,
        "reason": "no_tsconfig_or_lint_script_for_vue_vite",
    }


# Overrides a project vitest.config's `coverage.all: true` (report/enforce
# thresholds over every file matching `coverage.include`, even ones no
# executed test ever touched) for scoped, per-story runs. With this off,
# coverage is computed only over files actually loaded while running the
# story's own test files — the changed file(s) plus whatever they pull in
# — instead of the entire, largely-unrelated codebase. A story that only
# ever adds a few tests can otherwise never satisfy a repo-wide 80%
# threshold, no matter how well it covers its own change.
_SCOPED_COVERAGE_ARGS = ("--coverage.all=false",)


def _coverage_only_failure(result: dict) -> str | None:
    """None when `result` (a run_vue_tests/language_hooks.run outcome)
    wasn't a pure coverage-threshold miss; otherwise a short summary of
    which metrics fell short, e.g. "lines 53.4% < 80%; functions 17.4% <
    80%" — Story.coverage_gap_detail's value.

    Vitest exits non-zero for a coverage-threshold miss exactly the same
    way it does for a failed assertion, so `result["passed"]` alone can't
    tell them apart. This requires positive confirmation every test file
    actually passed (not just the absence of a "failed" line — a crash
    before any test ran wouldn't mention "failed" either) before treating
    the run as anything other than a real failure.
    """
    if result.get("passed"):
        return None
    # Vitest writes its "Test Files/Tests ... passed" summary to stdout but
    # its "ERROR: Coverage for X does not meet threshold" lines to stderr —
    # checking stdout alone (the original approach here) always finds zero
    # threshold misses and this function silently returns None for every
    # real coverage-only miss, indistinguishable from "not a coverage
    # issue." Both streams are checked for all three patterns, not just the
    # threshold one, since which stream carries "failed"/"passed" isn't
    # guaranteed stable across Vitest versions/configs either.
    combined = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part)
    if _VITEST_TESTS_FAILED_RE.search(combined):
        return None
    if not _VITEST_TESTS_PASSED_RE.search(combined):
        return None
    misses = _VITEST_COVERAGE_THRESHOLD_RE.findall(combined)
    if not misses:
        return None
    return "; ".join(f"{metric} {actual}% < {threshold}%" for metric, actual, threshold in misses)


def run_vue_tests(worktree_path: str, test_files: list[str]) -> dict:
    if not test_files:
        return {"passed": True, "skipped": True, "reason": "no_test_files"}
    result = language_hooks.run(
        ["npx", "vitest", "run", *_SCOPED_COVERAGE_ARGS, *test_files],
        cwd=worktree_path,
        timeout=VITEST_TIMEOUT_SECONDS,
    )
    detail = _coverage_only_failure(result)
    if detail:
        result = {**result, "coverage_gap": True, "coverage_gap_detail": detail}
    return result


def run_vue_all_tests(worktree_path: str) -> dict:
    """Whole-suite Vitest run — distinct from `run_vue_tests(..., [])`,
    which skips because an empty path list would select nothing."""
    return language_hooks.run(
        ["npx", "vitest", "run"],
        cwd=worktree_path,
        timeout=VITEST_TIMEOUT_SECONDS,
    )


def _format_vue_test_command(
    worktree_path: str,
    prefix: tuple[str, ...],
    requested_args: list[str],
    planned_test_targets: list[str],
) -> tuple[str | None, list[str] | None]:
    """Rewrite bare npm/vitest test invocations to the planned files.

    When the matched prefix is the short `npx vitest`, a lone leading
    `run` is treated as part of the tool invocation (not a path filter).
    Any other non-empty args mean the agent already scoped the command.
    """
    del worktree_path  # unused; Vitest takes paths as-is from the plan
    args = list(requested_args)
    if prefix == ("npx", "vitest") and args[:1] == ["run"]:
        args = args[1:]

    if args:
        return None, None  # already scoped — trust and run as-is

    if not planned_test_targets:
        return "BLOCKED: no planned test files in the approved plan.", None

    return None, ["npx", "vitest", "run", *_SCOPED_COVERAGE_ARGS, *planned_test_targets]


VUE_PLANNER_CONVENTIONS = """
Vue 3 + Vite planner conventions:
- Prefer modifying existing components, composables, Pinia stores, and pages under `src/` rather than introducing new frameworks or state libraries.
- Unit tests live as `*.spec.ts` (or under `tests/`); include matching test files in `files_to_modify` when behavior changes.
- Web-component design systems expose dash-cased custom-element tags (e.g. `<x-button>`) — those are custom elements, not Vue SFCs to register.
""".strip()


VUE_DEV_CONVENTIONS = """
Vue 3 + Vite conventions for this codebase:
- Use Vue 3 Composition API with `<script setup lang="ts">` in `.vue` SFCs; match surrounding files for Options API leftovers rather than converting them casually.
- State: Pinia stores under the project's existing `stores/` layout; shared logic in `composables/`. Do not add Vuex or a second state library.
- Dash-cased tags from a web-component design system are custom elements (Vite already treats them as such) — do not register them as Vue components.
- Routing and pages follow the existing `router/` + `pages/` (or equivalent) structure; keep new routes consistent with current patterns.
- Unit tests: Vitest + `@vue/test-utils`, named `*.spec.ts` under `tests/`. Prefer extending existing test helpers over inventing new harnesses.
- Run typecheck via `vue-tsc` and scoped Vitest for planned test files — do not invent a different test runner.
""".strip()


VUE_VERIFIER_REVIEW_CONVENTIONS = """
Vue 3 + Vite review notes:
- Generated `dist/` and `node_modules/` are not part of the change set — do not flag them as missing from the plan.
- Prefer typecheck (`vue-tsc`) and Vitest failures over subjective style nits when both are available.
""".strip()


def vue_vite_profile() -> LanguageProfile:
    return LanguageProfile(
        key="vue_vite",
        marker_files=("vite.config.ts", "vite.config.js", "vite.config.mts"),
        is_source=is_vue_source,
        is_test=is_vue_test,
        ensure_dependencies=ensure_npm_dependencies,
        analyze=run_vue_analyze,
        run_tests=run_vue_tests,
        run_all_tests=run_vue_all_tests,
        safe_command_prefixes=(
            ("npm", "ci"),
            ("npm", "install"),
            ("npm", "run", "lint"),
            ("npm", "run", "build"),
            ("npm", "run", "test"),
            ("npm", "test"),
            ("npx", "vitest", "run"),
            ("npx", "vitest"),
            ("npx", "vue-tsc"),
            ("npx", "eslint"),
        ),
        analyze_command_prefixes=(
            ("npx", "vue-tsc"),
            ("npm", "run", "lint"),
            ("npx", "eslint"),
        ),
        # Longer vitest prefixes first so `npx vitest run` matches before
        # the shorter `npx vitest` and bare invocations still get rewritten.
        test_command_prefixes=(
            ("npx", "vitest", "run"),
            ("npx", "vitest"),
            ("npm", "run", "test"),
            ("npm", "test"),
        ),
        format_test_command=_format_vue_test_command,
        planner_conventions=VUE_PLANNER_CONVENTIONS,
        dev_conventions=VUE_DEV_CONVENTIONS,
        verifier_review_conventions=VUE_VERIFIER_REVIEW_CONVENTIONS,
    )
