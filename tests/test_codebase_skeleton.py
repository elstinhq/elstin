"""elstin/tools/codebase_skeleton.py — the pre-computed repo map folded into
the planner brief. Pure filesystem; no git, no DB."""
from __future__ import annotations

import os

from elstin.tools.codebase_skeleton import build_codebase_skeleton, extract_declarations, render_skeleton, scan_repo


def _write(root: str, rel: str, content: str = "") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_extract_declarations_finds_top_level_only():
    source = (
        "import x\n"
        "class AuthService:\n"
        "    def login(self):\n"
        "        pass\n"
        "    class Inner:\n"
        "        pass\n"
        "def helper():\n"
        "    pass\n"
        "export const useAuth = () => {}\n"
        "export default function LoginPage() {}\n"
        "abstract class Base {}\n"
        "public final class Widget {}\n"
        "enum Mode { a, b }\n"
    )

    assert extract_declarations(source) == ["AuthService", "helper", "LoginPage", "Base", "Widget", "Mode", "useAuth"]


def test_scan_repo_uses_generic_extensions_when_no_profile_matches(tmp_path):
    _write(str(tmp_path), "app/service.py", "class Service:\n    pass\n")
    _write(str(tmp_path), "app/README.md", "# not source\n")
    _write(str(tmp_path), "node_modules/dep/index.js", "export class Dep {}\n")
    _write(str(tmp_path), ".hidden/x.py", "class Hidden: pass\n")

    files = scan_repo(str(tmp_path))

    assert files == {"app/service.py": ["Service"]}


def test_scan_repo_uses_detected_profile_source_predicates(tmp_path):
    """With a Dart project marker present, only what the Dart profile
    calls source/test counts — a stray .py at the root is not listed."""
    _write(str(tmp_path), "pubspec.yaml", "name: app\n")
    _write(str(tmp_path), "lib/auth/auth_service.dart", "class AuthService {}\nclass AuthError {}\n")
    _write(str(tmp_path), "test/auth_test.dart", "void main() {}\n")
    _write(str(tmp_path), "tool/script.py", "class Script: pass\n")

    files = scan_repo(str(tmp_path))

    assert files == {"lib/auth/auth_service.dart": ["AuthService", "AuthError"], "test/auth_test.dart": []}


def test_render_groups_by_directory_and_lists_declarations():
    rendered = render_skeleton({
        "lib/auth/auth_service.dart": ["AuthService", "AuthError"],
        "lib/auth/login_page.dart": ["LoginPage"],
        "lib/main.dart": [],
        "main.py": ["run"],
    })

    assert rendered.splitlines() == [
        "./: main.py [run]",
        "lib/: main.dart",
        "lib/auth/: auth_service.dart [AuthService, AuthError], login_page.dart [LoginPage]",
    ]


def test_render_truncates_on_a_line_boundary_and_counts_the_rest():
    files = {f"dir{i}/file{i}.py": [f"Thing{i}"] for i in range(50)}

    rendered = render_skeleton(files, max_chars=200)

    lines = rendered.splitlines()
    assert lines[-1].startswith("... (")
    assert "more source file(s) omitted" in lines[-1]
    assert sum(len(l) + 1 for l in lines[:-1]) <= 200


def test_build_codebase_skeleton_is_empty_for_a_repo_with_no_source(tmp_path):
    _write(str(tmp_path), "README.md", "hi\n")

    assert build_codebase_skeleton(str(tmp_path)) == ""


def test_build_codebase_skeleton_has_a_header_with_the_file_count(tmp_path):
    _write(str(tmp_path), "a.py", "class A: pass\n")
    _write(str(tmp_path), "b.py", "def b(): pass\n")

    skeleton = build_codebase_skeleton(str(tmp_path))

    assert skeleton.startswith("## Codebase skeleton (2 source files, pre-computed)")
    assert "./: a.py [A], b.py [b]" in skeleton
