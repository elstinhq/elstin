"""No database, no real npm/vitest — exercises Vue path classification,
TSC error parsing, and test-command rewriting against fixtures on disk."""
from __future__ import annotations

import os

from forge.tools import vue_tools


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def test_is_vue_source_and_test():
    assert vue_tools.is_vue_source("src/components/Foo.vue")
    assert vue_tools.is_vue_source("src/stores/order.ts")
    assert vue_tools.is_vue_source("src/composables/useFoo.ts")
    assert vue_tools.is_vue_source("components/Bar.vue")
    assert not vue_tools.is_vue_source("tests/unit/Foo.spec.ts")
    assert not vue_tools.is_vue_source("src/components/Foo.spec.ts")
    assert not vue_tools.is_vue_source("README.md")
    assert not vue_tools.is_vue_source("node_modules/vue/index.js")

    assert vue_tools.is_vue_test("tests/unit/Foo.spec.ts")
    assert vue_tools.is_vue_test("src/components/Foo.spec.ts")
    assert vue_tools.is_vue_test("src/utils/bar.test.ts")
    assert vue_tools.is_vue_test("src/__tests__/helper.ts")
    assert not vue_tools.is_vue_test("src/components/Foo.vue")
    assert not vue_tools.is_vue_test("node_modules/pkg/foo.spec.ts")


def test_parse_tsc_errors():
    output = (
        "src/components/Foo.vue(12,34): error TS2304: Cannot find name 'bar'.\n"
        "src/stores/order.ts(5,1): error TS1005: ';' expected.\n"
        "Found 2 errors.\n"
    )
    count, lines = vue_tools.parse_tsc_errors(output, "/repo")
    assert count == 2
    assert lines[0] == "src/components/Foo.vue:12:34: Cannot find name 'bar'."
    assert lines[1] == "src/stores/order.ts:5:1: ';' expected."


def test_parse_tsc_errors_absolute_paths(tmp_path):
    abs_file = str(tmp_path / "src" / "Foo.ts")
    output = f"{abs_file}(3,1): error TS2322: Type 'string' is not assignable to type 'number'.\n"
    count, lines = vue_tools.parse_tsc_errors(output, str(tmp_path))
    assert count == 1
    assert lines[0] == "src/Foo.ts:3:1: Type 'string' is not assignable to type 'number'."


def test_parse_tsc_errors_no_errors():
    count, lines = vue_tools.parse_tsc_errors("No errors found.\n", "/repo")
    assert count == 0
    assert lines == []


def test_format_vue_test_command_bare_rewrites_to_planned_files():
    err, cmd = vue_tools._format_vue_test_command(
        "/repo",
        ("npm", "test"),
        [],
        ["tests/unit/Foo.spec.ts", "tests/unit/Bar.spec.ts"],
    )
    assert err is None
    assert cmd == ["npx", "vitest", "run", "--coverage.all=false", "tests/unit/Foo.spec.ts", "tests/unit/Bar.spec.ts"]


def test_format_vue_test_command_npx_vitest_run_bare():
    err, cmd = vue_tools._format_vue_test_command(
        "/repo",
        ("npx", "vitest", "run"),
        [],
        ["tests/unit/Foo.spec.ts"],
    )
    assert err is None
    assert cmd == ["npx", "vitest", "run", "--coverage.all=false", "tests/unit/Foo.spec.ts"]


def test_format_vue_test_command_strips_leading_run_on_short_prefix():
    """If the short `npx vitest` prefix matched, a lone `run` arg is not
    a path filter — rewrite to planned files."""
    err, cmd = vue_tools._format_vue_test_command(
        "/repo",
        ("npx", "vitest"),
        ["run"],
        ["tests/unit/Foo.spec.ts"],
    )
    assert err is None
    assert cmd == ["npx", "vitest", "run", "--coverage.all=false", "tests/unit/Foo.spec.ts"]


def test_format_vue_test_command_already_scoped_passes_through():
    err, cmd = vue_tools._format_vue_test_command(
        "/repo",
        ("npx", "vitest", "run"),
        ["tests/unit/Explicit.spec.ts"],
        ["tests/unit/Foo.spec.ts"],
    )
    assert err is None
    assert cmd is None


def test_format_vue_test_command_blocks_without_planned_tests():
    err, cmd = vue_tools._format_vue_test_command("/repo", ("npm", "run", "test"), [], [])
    assert err is not None
    assert "BLOCKED" in err
    assert cmd is None


def test_ensure_npm_dependencies_prefers_ci_with_lockfile(tmp_path, monkeypatch):
    _write(str(tmp_path / "package-lock.json"), "{}")
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        return {"passed": True, "command": " ".join(cmd)}

    monkeypatch.setattr(vue_tools.language_hooks, "run", fake_run)
    result = vue_tools.ensure_npm_dependencies(str(tmp_path))
    assert result["passed"] is True
    assert calls == [["npm", "ci"]]


def test_ensure_npm_dependencies_falls_back_to_install(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        return {"passed": True, "command": " ".join(cmd)}

    monkeypatch.setattr(vue_tools.language_hooks, "run", fake_run)
    vue_tools.ensure_npm_dependencies(str(tmp_path))
    assert calls == [["npm", "install"]]


def test_run_vue_analyze_uses_vue_tsc_when_tsconfig_present(tmp_path, monkeypatch):
    _write(str(tmp_path / "tsconfig.json"), "{}")
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        return {
            "passed": False,
            "stdout": "src/Foo.ts(1,1): error TS2304: Cannot find name 'x'.\n",
            "stderr": "",
        }

    monkeypatch.setattr(vue_tools.language_hooks, "run", fake_run)
    result = vue_tools.run_vue_analyze(str(tmp_path), ["src/Foo.ts"])
    assert calls == [["npx", "vue-tsc", "-b", "--noEmit"]]
    assert result["passed"] is False
    assert result["error_count"] == 1
    assert result["error_lines"][0].startswith("src/Foo.ts:1:1:")


def test_run_vue_analyze_falls_back_to_lint(tmp_path, monkeypatch):
    _write(
        str(tmp_path / "package.json"),
        '{"scripts": {"lint": "eslint ."}}',
    )
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        return {"passed": True}

    monkeypatch.setattr(vue_tools.language_hooks, "run", fake_run)
    result = vue_tools.run_vue_analyze(str(tmp_path), [])
    assert calls == [["npm", "run", "lint"]]
    assert result["passed"] is True


def test_run_vue_analyze_skips_without_ts_or_lint(tmp_path):
    result = vue_tools.run_vue_analyze(str(tmp_path), [])
    assert result == {
        "passed": True,
        "skipped": True,
        "reason": "no_tsconfig_or_lint_script_for_vue_vite",
    }


def test_run_vue_tests_skips_empty_targets():
    assert vue_tools.run_vue_tests("/repo", []) == {
        "passed": True,
        "skipped": True,
        "reason": "no_test_files",
    }


def test_run_vue_tests_scopes_coverage_to_the_files_the_run_touches(monkeypatch):
    """A project vitest.config's `coverage.all: true` reports/enforces
    thresholds over every matched file, tested or not — a per-story run
    that only ever exercises a couple of test files would otherwise be
    held to a whole-codebase coverage bar it has no way to move. `--coverage.all=false`
    scopes the report to what this run actually touched."""
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        return {"passed": True}

    monkeypatch.setattr(vue_tools.language_hooks, "run", fake_run)
    vue_tools.run_vue_tests("/repo", ["tests/unit/Foo.spec.ts"])
    assert calls == [["npx", "vitest", "run", "--coverage.all=false", "tests/unit/Foo.spec.ts"]]


def test_coverage_only_failure_detects_threshold_miss_reported_on_stderr():
    """Regression: Vitest writes its "Test Files/Tests ... passed" summary
    to stdout but its "ERROR: Coverage for X does not meet threshold"
    lines to stderr — checking stdout alone (the original implementation)
    always found zero threshold misses and silently treated every real
    coverage-only miss as an unclassified failure instead."""
    result = {
        "passed": False,
        "stdout": "Test Files  1 passed (1)\n Tests  5 passed (5)\n",
        "stderr": (
            "ERROR: Coverage for lines (56.38%) does not meet global threshold (80%)\n"
            "ERROR: Coverage for statements (56.38%) does not meet global threshold (80%)\n"
        ),
    }
    detail = vue_tools._coverage_only_failure(result)
    assert detail == "lines 56.38% < 80%; statements 56.38% < 80%"


def test_coverage_only_failure_none_when_a_test_actually_failed():
    """A real test failure must never be misclassified as a coverage-only
    gap just because coverage-threshold lines also happen to be present."""
    result = {
        "passed": False,
        "stdout": "Test Files  1 failed (1)\n Tests  1 failed | 4 passed (5)\n",
        "stderr": "ERROR: Coverage for lines (56.38%) does not meet global threshold (80%)\n",
    }
    assert vue_tools._coverage_only_failure(result) is None


def test_vue_vite_profile_shape():
    profile = vue_tools.vue_vite_profile()
    assert profile.key == "vue_vite"
    assert "vite.config.ts" in profile.marker_files
    assert profile.run_all_tests is not None
    assert profile.verify is None
    assert "Pinia" in profile.dev_conventions
