"""forge/tools/planner_tools.py — the planner's own read-only Read/Grep/
Glob equivalents (no provider offers built-in tools, so forge supplies
these itself)."""
from __future__ import annotations

import os

from forge.tools.planner_tools import make_planner_tools


def _write(root: str, rel_path: str, content: str) -> None:
    abs_path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(content)


def test_read_file_tool_numbers_lines(tmp_path):
    _write(str(tmp_path), "a.py", "one\ntwo\nthree\n")
    functions, _schemas = make_planner_tools(project_path=str(tmp_path))

    result = functions["read_file_tool"]("a.py")

    assert result == "1\tone\n2\ttwo\n3\tthree"


def test_read_file_tool_missing_file_is_not_a_crash(tmp_path):
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["read_file_tool"]("nope.py")

    assert "not found" in result.lower()


def test_read_file_tool_blocks_path_traversal(tmp_path):
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["read_file_tool"]("../../etc/passwd")

    assert "BLOCKED" in result


def test_read_file_tool_windowed_read_keeps_lines_header_unnumbered(tmp_path):
    _write(str(tmp_path), "a.py", "\n".join(f"line{i}" for i in range(1, 20)) + "\n")
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["read_file_tool"]("a.py", offset=5, limit=2)

    assert result.startswith("[lines 5-6 of 19]")


def test_read_file_tool_caps_an_unwindowed_read_and_numbers_from_the_real_line(tmp_path):
    from forge.tools.grep import MAX_READ_LINES

    _write(str(tmp_path), "a.py", "\n".join(f"line{i}" for i in range(1, 601)) + "\n")
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["read_file_tool"]("a.py")

    header, _, body = result.partition("\n")
    assert header.startswith(f"[lines 1-{MAX_READ_LINES} of 600]")
    assert f"offset={MAX_READ_LINES + 1}" in header
    assert body.count("\n") == MAX_READ_LINES


def test_read_file_tool_rejects_a_limit_over_the_cap(tmp_path):
    _write(str(tmp_path), "a.py", "x\n" * 10)
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["read_file_tool"]("a.py", offset=1, limit=1000)

    assert result.startswith("BLOCKED")


def test_read_only_tools_set_matches_the_schemas():
    from forge.tools.planner_tools import READ_ONLY_TOOLS

    _functions, schemas = make_planner_tools(project_path="/tmp")
    assert READ_ONLY_TOOLS == {s["function"]["name"] for s in schemas}


def test_grep_tool_finds_matches(tmp_path):
    _write(str(tmp_path), "a.py", "def foo():\n    pass\n")
    _write(str(tmp_path), "b.py", "def bar():\n    foo()\n")
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["grep_tool"]("foo")

    assert "a.py:1:" in result
    assert "b.py:2:" in result


def test_grep_tool_no_matches():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        functions, _ = make_planner_tools(project_path=tmp)
        assert functions["grep_tool"]("nonexistent_symbol_xyz") == "No matches."


def test_find_files_tool_matches_glob(tmp_path):
    _write(str(tmp_path), "lib/a.dart", "")
    _write(str(tmp_path), "lib/sub/b.dart", "")
    _write(str(tmp_path), "test/a_test.dart", "")
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["find_files_tool"]("*.dart")

    assert "lib/a.dart" in result
    assert "lib/sub/b.dart" in result
    assert "test/a_test.dart" in result


def test_find_files_tool_excludes_default_dirs(tmp_path):
    _write(str(tmp_path), "node_modules/pkg/index.dart", "")
    _write(str(tmp_path), "lib/a.dart", "")
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    result = functions["find_files_tool"]("*.dart")

    assert "lib/a.dart" in result
    assert "node_modules" not in result


def test_schemas_match_function_names():
    _functions, schemas = make_planner_tools(project_path="/tmp")
    names = {s["function"]["name"] for s in schemas}
    assert names == {"read_file_tool", "grep_tool", "find_files_tool"}
