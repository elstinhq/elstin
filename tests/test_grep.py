"""elstin/tools/grep.py's grep_codebase and the two tools built on it
(dev's search_codebase_tool, the planner's grep_tool).

Regressions from a real dev run: `--max-count` is ripgrep's cap per FILE,
so "max_results=15" used to return a match per file across the whole repo
(136 hits, ~6k tokens, for one identifier), and a pattern starting with
"-" was handed to ripgrep as an option instead of a pattern.
"""
from __future__ import annotations

import os

import pytest

from elstin.tools.dev_tools import SEARCH_MAX_RESULTS, make_dev_tools
from elstin.tools.grep import MAX_MATCH_LINE_CHARS, grep_codebase
from elstin.tools.planner_tools import make_planner_tools


def _write(root, rel: str, content: str) -> None:
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _many_files(root, count: int, text: str = "needle") -> None:
    for i in range(count):
        _write(root, f"src/f{i:02d}.py", f"{text} = {i}\n")


def test_max_results_caps_the_total_not_each_file(tmp_path):
    _many_files(tmp_path, 30)

    matches = grep_codebase(str(tmp_path), "needle", max_results=5)

    assert len(matches) == 5


def test_matches_come_back_in_path_order(tmp_path):
    _many_files(tmp_path, 12)

    first = [m.file_path for m in grep_codebase(str(tmp_path), "needle", max_results=50)]

    assert first == sorted(first)
    assert first == [m.file_path for m in grep_codebase(str(tmp_path), "needle", max_results=50)]


@pytest.mark.parametrize("pattern", ["--verbose", "-> Widget"])
def test_a_pattern_starting_with_a_dash_is_a_pattern_not_an_option(tmp_path, pattern):
    _write(tmp_path, "cli.txt", f"run it with {pattern} please\n")

    matches = grep_codebase(str(tmp_path), pattern)

    assert [(m.file_path, m.line_number) for m in matches] == [("cli.txt", 1)]


def test_a_very_long_matching_line_comes_back_as_a_preview(tmp_path):
    _write(tmp_path, "bundle.min.js", "x" * 20_000 + "needle" + "y" * 20_000 + "\n")

    [match] = grep_codebase(str(tmp_path), "needle")

    assert len(match.line_text) < MAX_MATCH_LINE_CHARS + 100


def test_an_invalid_regex_is_an_error(tmp_path):
    _write(tmp_path, "a.py", "foo(\n")

    with pytest.raises(RuntimeError):
        grep_codebase(str(tmp_path), "foo(")


def _dev_tools(root):
    functions, _ = make_dev_tools(
        worktree_path=str(root), allowed_paths=set(), analyze_targets=[], allow_test_run=False, planned_test_files=[],
    )
    return functions


def test_search_codebase_tool_stops_at_its_cap_and_says_so(tmp_path):
    _many_files(tmp_path, 40)

    result = _dev_tools(tmp_path)["search_codebase_tool"]("needle")
    lines = result.splitlines()

    assert len([line for line in lines if line.startswith("src/")]) == SEARCH_MAX_RESULTS
    assert lines[0] == "src/f00.py:1: needle = 0"
    assert "showing the first" in lines[-1]


def test_search_codebase_tool_does_not_claim_a_cut_when_everything_fit(tmp_path):
    _many_files(tmp_path, SEARCH_MAX_RESULTS)

    result = _dev_tools(tmp_path)["search_codebase_tool"]("needle")

    assert len(result.splitlines()) == SEARCH_MAX_RESULTS
    assert "showing the first" not in result


def test_search_codebase_tool_reports_no_matches(tmp_path):
    _many_files(tmp_path, 2)

    assert _dev_tools(tmp_path)["search_codebase_tool"]("absent_symbol_xyz") == "No matches."


def test_planner_grep_tool_caps_the_total_and_only_says_truncated_when_it_was(tmp_path):
    _many_files(tmp_path, 8)
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    cut = functions["grep_tool"]("needle", max_results=3)
    whole = functions["grep_tool"]("needle", max_results=8)

    assert len([line for line in cut.splitlines() if line.startswith("src/")]) == 3
    assert "truncated" in cut
    assert len(whole.splitlines()) == 8
    assert "truncated" not in whole
