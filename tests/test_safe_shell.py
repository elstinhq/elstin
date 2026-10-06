"""elstin/tools/safe_shell.py — the run_command_tool allowlist. An allowed
prefix is not enough: git flags that write or read outside the worktree
must be refused even though `git diff` itself is allowed."""
from __future__ import annotations

import pytest

from elstin.tools import safe_shell
from elstin.tools.dev_tools import make_dev_tools


@pytest.mark.parametrize(
    "command",
    [
        "git diff --output=/tmp/x",
        "git diff --output /tmp/x",
        "git diff --output=relative.txt",
        "git diff --stat --output=/tmp/x",
        "git diff HEAD --output=/tmp/x",
        "git diff --out=/tmp/x",  # unambiguous-prefix spelling of --output
        "git diff -O/tmp/order",
        "git diff -O /tmp/order",
        "git diff -pO/tmp/order",  # short-flag cluster ending in -O<file>
        "git diff --no-index -- /etc/passwd /dev/null",
        "git diff /etc/passwd /dev/null",  # implicit --no-index
        "git diff ../secret.txt ../other.txt",
        "git diff ~/secret.txt ~/other.txt",
        "git add --pathspec-from-file=/etc/passwd",
        "git checkout --pathspec-from-file=/etc/passwd",
    ],
)
def test_git_flags_that_reach_outside_the_worktree_are_refused(command):
    assert not safe_shell.is_safe_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status --porcelain",
        "git diff",
        "git diff --stat",
        "git diff --cached --name-only",
        "git diff HEAD -- src/a.py",
        "git diff origin/main..HEAD",
        "git diff origin/main...HEAD --stat",
        "git diff --diff-filter=O",  # a capital O as a value is not -O
        "git diff -SFOO",  # pickaxe text is not -O
        "git diff -GOrder",
        "git diff -- --output=/tmp/x",  # after `--` it is a path, not a flag
        "git add src/a.py",
        "git checkout -- src/a.py",
    ],
)
def test_ordinary_git_commands_still_pass(command):
    assert safe_shell.is_safe_command(command)


def test_run_safe_command_refuses_output_and_writes_nothing(git_repo, tmp_path):
    target = tmp_path / "escaped.diff"
    (tmp_path / "widgets" / "README.md").write_text("changed\n")

    for command in (f"git diff --output={target}", f"git diff --output {target}"):
        result = safe_shell.run_safe_command(git_repo, command)

        assert result.returncode == -1
        assert result.stderr == f"Command execution blocked by safety policy: '{command}'"
        assert not target.exists()


def test_run_safe_command_still_runs_an_allowed_diff(git_repo, tmp_path):
    (tmp_path / "widgets" / "README.md").write_text("changed\n")

    result = safe_shell.run_safe_command(git_repo, "git diff --stat")

    assert result.returncode == 0
    assert "README.md" in result.stdout


def test_run_command_tool_reports_the_policy_refusal(git_repo, tmp_path):
    target = tmp_path / "escaped.diff"
    functions, _schemas = make_dev_tools(
        worktree_path=git_repo, allowed_paths={"README.md"}, analyze_targets=[], allow_test_run=False, planned_test_files=[],
    )

    out = functions["run_command_tool"](f"git diff --output={target}")

    assert "Return Code: -1" in out
    assert "blocked by safety policy" in out
    assert not target.exists()
