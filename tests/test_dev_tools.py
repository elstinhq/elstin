"""forge/tools/dev_tools.py — the read cap and the tool-class sets the
dev loop hands forge/agents/loop.py. No git, no toolchain: make_dev_tools
is bound to a plain temp dir with a wide-open plan scope."""
from __future__ import annotations

import os

from forge.tools.dev_tools import PROGRESS_TOOLS, READ_ONLY_TOOLS, TOOL_SCHEMAS, make_dev_tools
from forge.tools.grep import MAX_READ_LINES


def _tools(root: str):
    functions, _schemas = make_dev_tools(
        worktree_path=root, allowed_paths={"a.py"}, analyze_targets=[], allow_test_run=False, planned_test_files=[],
    )
    return functions


def _write(root: str, rel: str, lines: int) -> None:
    with open(os.path.join(root, rel), "w") as f:
        f.write("".join(f"line {i}\n" for i in range(1, lines + 1)))


def test_read_file_tool_serves_a_small_file_whole(tmp_path):
    _write(str(tmp_path), "a.py", 3)

    assert _tools(str(tmp_path))["read_file_tool"]("a.py") == "line 1\nline 2\nline 3\n"


def test_read_file_tool_caps_an_unwindowed_read_at_250_lines_and_says_how_to_continue(tmp_path):
    _write(str(tmp_path), "big.py", 1000)

    result = _tools(str(tmp_path))["read_file_tool"]("big.py")

    header, _, body = result.partition("\n")
    assert header.startswith(f"[lines 1-{MAX_READ_LINES} of 1000]")
    assert f"offset={MAX_READ_LINES + 1}" in header
    assert body.count("\n") == MAX_READ_LINES
    assert body.endswith(f"line {MAX_READ_LINES}\n")


def test_read_file_tool_rejects_a_limit_over_250(tmp_path):
    _write(str(tmp_path), "big.py", 1000)

    result = _tools(str(tmp_path))["read_file_tool"]("big.py", offset=1, limit=400)

    assert result.startswith("BLOCKED")
    assert str(MAX_READ_LINES) in result
    assert "line 1" not in result  # nothing served


def test_read_file_tool_windowed_read_within_cap_is_exact(tmp_path):
    _write(str(tmp_path), "big.py", 1000)

    result = _tools(str(tmp_path))["read_file_tool"]("big.py", offset=500, limit=3)

    assert result == "[lines 500-502 of 1000]\nline 500\nline 501\nline 502\n"


def test_read_file_tool_window_running_past_eof_is_not_flagged_as_capped(tmp_path):
    _write(str(tmp_path), "big.py", 300)

    result = _tools(str(tmp_path))["read_file_tool"]("big.py", offset=200)

    assert result.startswith("[lines 200-300 of 300]\n")
    assert "capped" not in result


def test_tool_class_sets_name_real_tools_and_do_not_overlap():
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}

    assert READ_ONLY_TOOLS <= names
    assert PROGRESS_TOOLS <= names
    assert not (READ_ONLY_TOOLS & PROGRESS_TOOLS)
    # The compiler check may write build output, so it must run alone.
    assert "analyze_changed_files_tool" not in READ_ONLY_TOOLS
    assert "run_command_tool" not in READ_ONLY_TOOLS


def test_read_past_the_end_of_a_file_says_so(tmp_path):
    """Regression: an offset beyond EOF produced "[lines 5000-300 of 300]"
    with an empty body, which reads as a valid window. Paging makes this
    reachable in ordinary use."""
    _write(str(tmp_path), "big.py", 300)

    result = _tools(str(tmp_path))["read_file_tool"]("big.py", offset=5000)

    assert result == "[file has 300 line(s); offset 5000 is past the end]"


# --- finding files: what dev used to attempt with ls/find ---------------


def _touch(root, *rels: str) -> None:
    for rel in rels:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x\n")


def test_find_files_tool_lists_a_folder_by_globbing_under_it(tmp_path):
    """The exact ask that was refused three times in a failed run:
    `ls pricing-api/src/main/resources/db/migration`."""
    root = str(tmp_path)
    _touch(root, "api/db/migration/V1_1__create.sql", "api/db/migration/V1_2__seed.sql", "api/application.yaml")

    result = _tools(root)["find_files_tool"]("api/db/migration/*")

    assert result.splitlines() == ["api/db/migration/V1_1__create.sql", "api/db/migration/V1_2__seed.sql"]


def test_find_files_tool_finds_by_name_anywhere_and_skips_dependency_and_hidden_folders(tmp_path):
    root = str(tmp_path)
    _touch(root, "src/a/Price.java", "src/b/Price.java", "node_modules/x/Price.java", ".git/Price.java", "build/Price.java")

    result = _tools(root)["find_files_tool"]("Price.java")

    assert result.splitlines() == ["src/a/Price.java", "src/b/Price.java"]


def test_find_files_tool_sees_a_file_created_during_the_run(tmp_path):
    root = str(tmp_path)
    tools = _tools(root)
    assert tools["find_files_tool"]("*.tsx") == "No files matched."

    _touch(root, "web/New.tsx")

    assert tools["find_files_tool"]("*.tsx") == "web/New.tsx"


def test_find_files_tool_caps_its_output_and_says_so(tmp_path):
    root = str(tmp_path)
    _touch(root, *(f"d/f{i}.txt" for i in range(10)))

    result = _tools(root)["find_files_tool"]("*.txt", 3)

    lines = result.splitlines()
    assert lines[:3] == ["d/f0.txt", "d/f1.txt", "d/f2.txt"] and "truncated" in lines[3]


def test_find_files_tool_is_offered_read_only_and_the_shell_says_it_cannot_list():
    schemas = {s["function"]["name"]: s["function"] for s in TOOL_SCHEMAS}

    assert "find_files_tool" in READ_ONLY_TOOLS and "find_files_tool" in schemas
    assert "cannot list" in schemas["run_command_tool"]["description"]
    assert "find_files_tool" in schemas["run_command_tool"]["description"]


# --- a refused command says what to use instead --------------------------


def _run(tmp_path, command: str) -> str:
    return _tools(str(tmp_path))["run_command_tool"](command)


def test_a_refused_ls_points_at_find_files_tool(tmp_path):
    result = _run(tmp_path, "ls pricing-api/src/main/resources/db/migration")

    assert result.startswith("Return Code: -1")
    assert "blocked by safety policy" in result
    assert "find_files_tool" in result and "some/folder/*" in result


def test_a_refused_find_with_a_pipe_gets_both_the_list_hint_and_the_pipe_rule(tmp_path):
    result = _run(tmp_path, "find src -maxdepth 3 -type f | sort")

    assert "find_files_tool" in result and "Pipes, redirects and command chaining are never allowed" in result


def test_a_refused_grep_points_at_search_and_read(tmp_path):
    result = _run(tmp_path, "grep -rn currency src")

    assert "search_codebase_tool" in result and "read_file_tool" in result


def test_a_refused_git_history_command_explains_what_git_does_run(tmp_path):
    result = _run(tmp_path, "git show HEAD:src/PriceRecord.java")

    assert "git status, git diff" in result and "read_file_tool" in result


def test_an_unknown_refused_command_gets_the_general_guidance(tmp_path):
    result = _run(tmp_path, "curl https://example.com")

    assert "build, test and codegen commands" in result and "find_files_tool" in result


def test_a_command_that_ran_is_not_decorated_with_a_hint(tmp_path):
    result = _run(tmp_path, "git status")  # allowed; fails here only because tmp_path is not a repo

    assert "blocked by safety policy" not in result
    assert "find_files_tool" not in result
