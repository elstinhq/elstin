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
