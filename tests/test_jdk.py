"""forge/tools/jdk.py — which JDK a Java project is built with, and
forge/tools/java_tools.py's toolchain-failure check. One story: the project
targets Java 21, forge built it on JDK 26, javac crashed, and three passes
went on "fixing" working code. Fake JDK homes on disk; no JVM runs."""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.tools import java_tools, jdk, language_hooks


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_jdk(root, name: str, version: str) -> str:
    home = root / name
    _write(home / "release", f'IMPLEMENTOR="Test"\nJAVA_VERSION="{version}"\n')
    _write(home / "bin" / "java", "#!/bin/sh\n")
    return str(home.resolve())


@pytest.fixture()
def jdks(tmp_path, monkeypatch):
    """JDK 21 (two patches) and 26 installed, JAVA_HOME unset — this
    machine's shape."""
    root = tmp_path / "jvms"
    homes = {
        "21": _fake_jdk(root, "temurin-21.0.12", "21.0.12"),
        "21-old": _fake_jdk(root, "temurin-21.0.3", "21.0.3"),
        "26": _fake_jdk(root, "openjdk-26", "26.0.2.1"),
    }
    monkeypatch.setattr(jdk, "SEARCH_GLOBS", (str(root / "*"),))
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setitem(jdk._scan, "at", 0.0)
    return homes


@pytest.mark.parametrize("version, major", [("21", 21), ("21.0.12+7", 21), ("1.8", 8), ("1.8.0_392", 8), ("1_8", 8), ("26.0.2.1", 26)])
def test_major_versions(version, major):
    assert jdk.major_of(version) == major


@pytest.mark.parametrize("files, expected", [
    ({"pom.xml": "<properties><java.version>21</java.version></properties>"}, 21),
    ({"pom.xml": "<java.version>17</java.version><maven.compiler.release>21</maven.compiler.release>"}, 21),
    ({"pom.xml": "<maven.compiler.source>1.8</maven.compiler.source>"}, 8),
    ({"pom.xml": "<release>${java.version}</release><java.version>21</java.version>"}, 21),
    ({"build.gradle.kts": "java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }"}, 17),
    ({"build.gradle": "sourceCompatibility = JavaVersion.VERSION_1_8"}, 8),
    ({"pom.xml": "<project/>", ".java-version": "21.0.2\n"}, 21),
    ({"pom.xml": "<project/>"}, None),
])
def test_the_version_a_project_states(tmp_path, files, expected):
    for name, text in files.items():
        _write(tmp_path / name, text)
    assert jdk.required_java_version(str(tmp_path)) == expected


def test_the_newest_installed_jdk_of_the_stated_version_is_used(tmp_path, jdks):
    _write(tmp_path / "pom.xml", "<java.version>21</java.version>")

    env = jdk.java_env(str(tmp_path))

    assert env["JAVA_HOME"] == jdks["21"]
    assert env["PATH"].startswith(os.path.join(jdks["21"], "bin") + os.pathsep)
    assert jdk.describe(str(tmp_path)) == f"The project targets Java 21; forge builds it with {jdks['21']}."


def test_no_stated_version_or_none_installed_leaves_the_environment_alone(tmp_path, jdks):
    _write(tmp_path / "pom.xml", "<java.version>17</java.version>")
    assert jdk.java_env(str(tmp_path)) == {}
    assert "no JDK 17 is installed where forge looks (installed: 21, 26) — install one, then Retry" in jdk.describe(str(tmp_path))

    _write(tmp_path / "pom.xml", "<project/>")
    assert jdk.java_env(str(tmp_path)) == {}


def test_every_command_in_a_java_worktree_runs_on_that_jdk(tmp_path, jdks):
    """The build check's commands (language_hooks.run) and the developer
    step's own (safe_shell) both get it."""
    from forge.tools import safe_shell

    _write(tmp_path / "pom.xml", "<java.version>21</java.version>")
    _write(tmp_path / "mvnw", '#!/bin/sh\necho "$JAVA_HOME"\n')
    os.chmod(tmp_path / "mvnw", 0o755)

    assert language_hooks.run(["./mvnw", "compile"], cwd=str(tmp_path), timeout=10)["stdout"].strip() == jdks["21"]
    assert safe_shell.run_safe_command(str(tmp_path), "./mvnw compile").stdout.strip() == jdks["21"]
    # No profile adds anything there: the minimal environment, with no JDK pinned.
    assert language_hooks.profile_command_env(str(tmp_path / "elsewhere")).get("JAVA_HOME") is None


# --- a failure that is the toolchain, not the code ------------------------------

_JDK_CRASH_OUTPUT = (
    "[INFO] BUILD FAILURE\n[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.8.1:compile "
    "(default-compile) on project mono-apiservice: Fatal error compiling: java.lang.ExceptionInInitializerError: "
    "com.sun.tools.javac.code.TypeTag :: UNKNOWN -> [Help 1]\n"
)


def test_a_javac_crash_is_the_environment(tmp_path, jdks):
    _write(tmp_path / "pom.xml", "<java.version>21</java.version>")

    reason = java_tools.java_environment_problem(str(tmp_path), {"passed": False, "error_count": 0, "stdout": _JDK_CRASH_OUTPUT})

    assert reason.startswith(
        "The Java build fails before compiling any source: Fatal error compiling: java.lang.ExceptionInInitializerError: "
        "com.sun.tools.javac.code.TypeTag :: UNKNOWN"
    )
    assert reason.endswith(f"The project targets Java 21; forge builds it with {jdks['21']}.")


@pytest.mark.parametrize("result", [
    {"passed": True, "stdout": _JDK_CRASH_OUTPUT},  # passed
    {"passed": False, "error_count": 2, "stdout": _JDK_CRASH_OUTPUT},  # a compiler error in the source is the code's
    {"passed": False, "error_count": 0, "stdout": "[ERROR] Failed to execute goal ... Could not resolve dependencies"},
])
def test_anything_else_is_the_codes(tmp_path, result):
    assert java_tools.java_environment_problem(str(tmp_path), result) is None


def test_the_build_check_flags_it(tmp_path, jdks):
    """_default_verify marks the issue; graph.gate_node stops on the mark."""
    _write(tmp_path / "pom.xml", "<java.version>21</java.version>")
    import dataclasses

    profile = dataclasses.replace(
        java_tools.java_maven_profile(),
        ensure_dependencies=lambda path: {"passed": True},
        analyze=lambda path, targets: {"passed": False, "error_count": 0, "stdout": _JDK_CRASH_OUTPUT},
    )
    ctx = language_hooks.VerifyContext(
        worktree_path=str(tmp_path), plan={}, changed=set(), dev_targets=set(), analyze_targets=["A.java"],
        test_targets=[], run_id=None, prior_state=None,
    )

    result = language_hooks.run_profile_verify(profile, ctx)

    assert result["passed"] is False
    assert "TypeTag :: UNKNOWN" in result["issues"][0]["environment"]


# --- build output is never committed --------------------------------------------


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def test_build_output_is_excluded_in_every_worktree_of_the_clone(git_repo, tmp_path):
    """One story: no .gitignore on main, and 168 files under target/ were
    staged for the epic's pull request."""
    from forge.tools.git_tools import list_changed_files
    from forge.tools.worktree import exclude_build_output

    with open(os.path.join(git_repo, "pom.xml"), "w") as handle:
        handle.write("<project/>")
    _git(git_repo, "add", "pom.xml")
    _git(git_repo, "commit", "-q", "-m", "pom")
    info_exclude = os.path.join(git_repo, ".git", "info", "exclude")
    with open(info_exclude, "a") as handle:
        handle.write("*.local\n")  # someone's own line survives
    worktree = str(tmp_path / "wt")
    _git(git_repo, "worktree", "add", "-q", "-b", "feat/x", worktree)

    assert exclude_build_output(git_repo, (worktree,)) == ["target/"]
    assert exclude_build_output(git_repo, (worktree,)) == ["target/"]  # rewritten, not appended

    os.makedirs(os.path.join(worktree, "target", "classes"))
    with open(os.path.join(worktree, "target", "classes", "application.yaml"), "w") as handle:
        handle.write("x: 1\n")
    with open(os.path.join(worktree, "Search.java"), "w") as handle:
        handle.write("class Search {}\n")
    assert list_changed_files(worktree) == ["Search.java"]
    text = open(info_exclude).read()
    assert "*.local\n" in text and text.count("# >>> code_forge") == 1 and "\ntarget/\n" in text
