"""Vue 3 + Vite `vue-tsc` / Vitest invocation, and the `LanguageProfile`
that wires it into forge/tools/language_hooks.py's registry.

Modeled on apps like mono-adminportal: Vite at the root, TypeScript via
`vue-tsc`, unit tests via Vitest, npm as the package manager. Uses the
shared default verify pipeline (ensure_dependencies -> analyze ->
run_tests) — no codegen/DI step between install and typecheck.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile

from forge.tools import language_hooks
from forge.tools.git_tools import list_changed_files, read_file_at_head, stage_files
from forge.tools.language_hooks import LanguageProfile

NPM_DEPENDENCY_TIMEOUT_SECONDS = 300
VUE_TSC_TIMEOUT_SECONDS = 180
ESLINT_TIMEOUT_SECONDS = 180

logger = logging.getLogger("forge.vue_tools")
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


# --- ESLint on what the story changed ------------------------------------------
#
# A repo's lint rules (Prettier through ESLint, say) are its standard, and
# vue-tsc does not check them: one story reached its pull request with 22
# prettier/prettier errors. But a repo is not necessarily lint-clean — one
# portal has 17,000 errors in 82 older files — so a story is held to its own
# changes, measured against each file's committed version (HEAD, the epic
# branch the story started from): a file it creates, or one that was clean,
# is auto-formatted and must stay clean; an older file is never reformatted
# wholesale (the repo's guide forbids drive-by rewrites) and only fails when
# the story added errors to it.

_ESLINT_CONFIG_FILES = (
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
    ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml",
)
_LINTABLE_SUFFIXES = (".vue", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_MAX_LINT_ERROR_LINES = 25
# {(worktree, HEAD sha, path): error count}: a HEAD version never changes.
_baseline_cache: dict[tuple[str, str, str], int] = {}


def _has_eslint(worktree_path: str) -> bool:
    if any(os.path.isfile(os.path.join(worktree_path, name)) for name in _ESLINT_CONFIG_FILES):
        return True
    try:
        with open(os.path.join(worktree_path, "package.json"), encoding="utf-8") as handle:
            return "eslintConfig" in json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def _lintable(worktree_path: str, paths: list[str]) -> list[str]:
    return sorted(
        path for path in paths
        if path.endswith(_LINTABLE_SUFFIXES) and os.path.isfile(os.path.join(worktree_path, path))
    )


def _eslint_errors(worktree_path: str, args: list[str], *, stdin: str | None = None) -> dict[str, list[dict]] | None:
    """{relative path: [error message, ...]} from `eslint --format json`
    (severity 2 only), or None when ESLint did not produce a report. The
    report goes to a file: it carries every file's source, and the shared
    runner keeps only the tail of stdout."""
    fd, report_path = tempfile.mkstemp(prefix="forge-eslint-", suffix=".json")
    os.close(fd)
    try:
        result = language_hooks.run(
            ["npx", "eslint", "--format", "json", "--output-file", report_path, *args],
            cwd=worktree_path, timeout=ESLINT_TIMEOUT_SECONDS, input=stdin,
        )
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("eslint produced no report in %s: %s", worktree_path, (locals().get("result") or {}).get("stderr", "")[-500:])
        return None
    finally:
        try:
            os.remove(report_path)
        except OSError:
            pass
    errors: dict[str, list[dict]] = {}
    for entry in report if isinstance(report, list) else []:
        path = entry.get("filePath") or ""
        rel = _norm(os.path.relpath(path, worktree_path)) if os.path.isabs(path) else _norm(path)
        errors[rel] = [m for m in entry.get("messages") or [] if m.get("severity") == 2]
    return errors


def _baseline_errors(worktree_path: str, path: str) -> int | None:
    """Lint errors in `path` as committed (HEAD), or None for a file the
    story created."""
    head = language_hooks.run(["git", "rev-parse", "HEAD"], cwd=worktree_path, timeout=30).get("stdout", "").strip()
    key = (worktree_path, head, path)
    if key in _baseline_cache:
        return _baseline_cache[key]
    content = read_file_at_head(worktree_path, path)
    if content is None:
        return None
    report = _eslint_errors(worktree_path, ["--stdin", "--stdin-filename", path], stdin=content)
    count = len((report or {}).get(path, [])) if report is not None else 0
    _baseline_cache[key] = count
    return count


def vue_format_changed_files(worktree_path: str, changed_files: list[str]) -> dict:
    """after_dev_changes: `eslint --fix` over the files the developer step
    changed that it created or that were lint-clean before — formatting done
    by the tool rather than spent on a fix round. Older files with existing
    errors are left as they are."""
    if not _has_eslint(worktree_path):
        return {"passed": True, "skipped": True, "reason": "no_eslint_config"}
    files = _lintable(worktree_path, changed_files)
    fixable = [path for path in files if not _baseline_errors(worktree_path, path)]
    if not fixable:
        return {"passed": True, "skipped": True, "reason": "nothing_to_format"}
    result = language_hooks.run(["npx", "eslint", "--fix", *fixable], cwd=worktree_path, timeout=ESLINT_TIMEOUT_SECONDS)
    stage_files(worktree_path, fixable)
    return {"passed": True, "skipped": False, "formatted": fixable, "eslint_exit": result.get("exit_code")}


def lint_story_changes(worktree_path: str) -> tuple[int, list[str]]:
    """(new error count, error lines) for the story's changed files: every
    error in a file it created or that was clean, and — for an older file
    that already had errors — the file's errors when the story added any."""
    if not _has_eslint(worktree_path):
        return 0, []
    files = _lintable(worktree_path, list_changed_files(worktree_path))
    if not files:
        return 0, []
    current = _eslint_errors(worktree_path, files)
    if current is None:
        return 0, []  # ESLint could not run; vue-tsc still gates the change
    count, lines = 0, []
    for path in files:
        errors = current.get(path, [])
        before = _baseline_errors(worktree_path, path) or 0
        if len(errors) <= before:
            continue
        count += len(errors) - before
        note = f" (the file had {before} before this change)" if before else ""
        for message in errors:
            lines.append(
                f"{path}:{message.get('line')}:{message.get('column')}: {message.get('message')} "
                f"({message.get('ruleId')}){note}"
            )
    return count, lines[:_MAX_LINT_ERROR_LINES]


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
    """Typecheck with `vue-tsc` when a TS project is present, plus ESLint
    over what the story changed (`lint_story_changes`); otherwise fall back
    to `npm run lint` if that script exists. `targets` are unused — the
    checkers are project-wide, and the lint scope is the story's changes —
    but kept for LanguageProfile API compatibility."""
    del targets  # project-wide checkers; path list is for dispatch only
    if _has_tsconfig(worktree_path):
        result = language_hooks.run(
            ["npx", "vue-tsc", "-b", "--noEmit"],
            cwd=worktree_path,
            timeout=VUE_TSC_TIMEOUT_SECONDS,
        )
        combined = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part)
        error_count, error_lines = parse_tsc_errors(combined, worktree_path)
        # The repo's lint rules too, on what the story changed (see
        # lint_story_changes) — vue-tsc does not check them.
        lint_count, lint_lines = lint_story_changes(worktree_path)
        return {
            **result,
            "passed": result.get("passed", False) and error_count == 0 and lint_count == 0,
            "error_count": error_count + lint_count,
            "error_lines": (error_lines + lint_lines)[:_MAX_LINT_ERROR_LINES * 2],
            "lint_error_count": lint_count,
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
- Adding or upgrading a dependency: do not write its name, version or API from memory. Run `npm view <pkg> version` to confirm the package exists and get its current version, `npm install <pkg>` to add it, then read `node_modules/<pkg>/README.md` and its `.d.ts` types before writing code against it. If `web_search_tool` / `web_fetch_tool` are available, use them to find and read the package's current documentation. Then let `vue-tsc` confirm the usage.
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
            # Read-only registry lookup — lets the agent check a package's
            # real name, latest version and dist-tags instead of guessing.
            ("npm", "view"),
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
        test_command_hint="npx vitest run <path/to/the.spec.ts>",
        format_test_command=_format_vue_test_command,
        planner_conventions=VUE_PLANNER_CONVENTIONS,
        dev_conventions=VUE_DEV_CONVENTIONS,
        verifier_review_conventions=VUE_VERIFIER_REVIEW_CONVENTIONS,
        build_output=("node_modules/", "coverage/", "dist/"),
        after_dev_changes=vue_format_changed_files,
    )
