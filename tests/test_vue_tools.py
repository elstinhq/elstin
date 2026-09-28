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


# --- ESLint on what the story changed ---------------------------------------------


def _lint_repo(git_repo: str, monkeypatch, errors_now: dict[str, int], errors_at_head: dict[str, int]):
    """A Vue repo with ESLint, and a fake ESLint: `errors_now` for the files as
    they are, `errors_at_head` for their committed versions (read on stdin)."""
    import subprocess

    _write(os.path.join(git_repo, "eslint.config.js"), "export default [];\n")
    for path in ("src/Old.vue", "src/Clean.ts", "src/Legacy.vue"):
        _write(os.path.join(git_repo, path), "x\n")
    subprocess.run(["git", "-C", git_repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", git_repo, "commit", "-q", "-m", "base"], check=True)
    for path in ("src/Old.vue", "src/Clean.ts", "src/Legacy.vue", "src/New.ts"):
        _write(os.path.join(git_repo, path), "changed\n")
    fixed: list[list[str]] = []

    def fake_eslint(worktree, args, *, stdin=None):
        if stdin is not None:
            path = args[args.index("--stdin-filename") + 1]
            return {path: [{"line": 1, "column": 1, "message": "old", "ruleId": "r"}] * errors_at_head.get(path, 0)}
        return {p: [{"line": 2, "column": 1, "message": "Insert `;`", "ruleId": "prettier/prettier"}] * errors_now.get(p, 0) for p in args}

    def fake_run(cmd, *, cwd, timeout, input=None):
        if cmd[:3] == ["npx", "eslint", "--fix"]:
            fixed.append(cmd[3:])
            return {"passed": True, "exit_code": 0}
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)  # git rev-parse HEAD
        return {"passed": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr}

    vue_tools._baseline_cache.clear()
    monkeypatch.setattr(vue_tools, "_eslint_errors", fake_eslint)
    monkeypatch.setattr(vue_tools.language_hooks, "run", fake_run)
    return fixed


def test_a_story_is_held_to_its_own_lint_errors_not_the_repos(git_repo, monkeypatch):
    """MKT2-866 reached its PR with prettier errors; the MKT portal also has
    17,000 errors in older files a story may touch. New and previously clean
    files must be clean; an older file fails only when the story added to it."""
    _lint_repo(
        git_repo, monkeypatch,
        errors_now={"src/New.ts": 2, "src/Clean.ts": 1, "src/Old.vue": 3, "src/Legacy.vue": 5},
        errors_at_head={"src/Old.vue": 3, "src/Legacy.vue": 4},
    )

    count, lines = vue_tools.lint_story_changes(git_repo)

    assert count == 2 + 1 + 1  # New.ts, Clean.ts, and Legacy.vue's one extra — not Old.vue's existing three
    assert any(line.startswith("src/New.ts:2:1: Insert `;` (prettier/prettier)") for line in lines)
    assert any("src/Legacy.vue:2:1" in line and "(the file had 4 before this change)" in line for line in lines)
    assert not any(line.startswith("src/Old.vue") for line in lines)


def test_only_new_or_previously_clean_files_are_auto_formatted(git_repo, monkeypatch):
    """Formatting is the tool's job, not a fix round's — but reformatting a
    whole older file would be the drive-by rewrite the repo's guide forbids."""
    fixed = _lint_repo(git_repo, monkeypatch, errors_now={}, errors_at_head={"src/Old.vue": 3, "src/Legacy.vue": 4})

    result = vue_tools.vue_format_changed_files(git_repo, ["src/Old.vue", "src/Clean.ts", "src/Legacy.vue", "src/New.ts", "README.md"])

    assert fixed == [["src/Clean.ts", "src/New.ts"]]
    assert result["formatted"] == ["src/Clean.ts", "src/New.ts"]


def test_the_analyze_step_fails_on_the_storys_lint_errors(tmp_path, monkeypatch):
    _write(str(tmp_path / "tsconfig.json"), "{}")
    monkeypatch.setattr(vue_tools.language_hooks, "run", lambda cmd, *, cwd, timeout, input=None: {"passed": True, "stdout": "", "stderr": ""})
    monkeypatch.setattr(vue_tools, "lint_story_changes", lambda path: (1, ["src/New.ts:2:1: Insert `;` (prettier/prettier)"]))

    result = vue_tools.run_vue_analyze(str(tmp_path), ["src/New.ts"])

    assert result["passed"] is False and result["error_count"] == 1 and result["lint_error_count"] == 1
    assert result["error_lines"] == ["src/New.ts:2:1: Insert `;` (prettier/prettier)"]


def test_no_eslint_config_means_no_lint(tmp_path):
    assert vue_tools.lint_story_changes(str(tmp_path)) == (0, [])
    assert vue_tools.vue_format_changed_files(str(tmp_path), ["src/a.ts"])["skipped"] is True
