"""No database, no real Maven/Gradle invocation — exercises the pure
parsing/classification logic (javac error-line regex, surefire XML,
FQCN resolution, wrapper preference) against fixture files on disk."""
from __future__ import annotations

import os

from forge.tools import java_tools


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def test_is_java_source_and_test():
    assert java_tools.is_java_source("src/main/java/com/example/Foo.java")
    assert not java_tools.is_java_source("src/test/java/com/example/FooTest.java")
    assert not java_tools.is_java_source("src/main/resources/application.yaml")

    assert java_tools.is_java_test("src/test/java/com/example/FooTest.java")
    assert not java_tools.is_java_test("src/main/java/com/example/Foo.java")
    assert not java_tools.is_java_test("src/test/java/com/example/TestHelper.java")  # doesn't end in Test.java


def test_maven_wrapper_prefers_mvnw(tmp_path):
    assert java_tools._maven_wrapper(str(tmp_path)) == "mvn"
    _write(str(tmp_path / "mvnw"), "#!/bin/sh\n")
    assert java_tools._maven_wrapper(str(tmp_path)) == "./mvnw"


def test_fqcn_for_java_file_reads_package_declaration(tmp_path):
    rel = "src/test/java/com/example/order/FooTest.java"
    _write(
        str(tmp_path / rel),
        "package com.example.order;\n\nclass FooTest {}\n",
    )
    assert java_tools.fqcn_for_java_file(str(tmp_path), rel) == "com.example.order.FooTest"


def test_fqcn_for_java_file_falls_back_to_bare_class_name_without_package(tmp_path):
    rel = "src/test/java/FooTest.java"
    _write(str(tmp_path / rel), "class FooTest {}\n")
    assert java_tools.fqcn_for_java_file(str(tmp_path), rel) == "FooTest"


def test_fqcn_for_java_file_missing_file_returns_none(tmp_path):
    assert java_tools.fqcn_for_java_file(str(tmp_path), "does/not/exist/FooTest.java") is None


def test_parse_javac_errors():
    output = (
        "[INFO] Compiling 3 source files\n"
        "[ERROR] /repo/src/main/java/com/example/Foo.java:[12,34] cannot find symbol\n"
        "[ERROR]   symbol:   variable bar\n"
        "[ERROR] /repo/src/main/java/com/example/Bar.java:[5,1] ';' expected\n"
        "[INFO] BUILD FAILURE\n"
    )
    count, lines = java_tools._parse_javac_errors(output, "/repo")
    assert count == 2
    assert lines[0] == "src/main/java/com/example/Foo.java:12:34: cannot find symbol"
    assert lines[1] == "src/main/java/com/example/Bar.java:5:1: ';' expected"


def test_parse_javac_errors_no_errors():
    count, lines = java_tools._parse_javac_errors("[INFO] BUILD SUCCESS\n", "/repo")
    assert count == 0
    assert lines == []


_SUREFIRE_PASS = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.FooTest" tests="2" errors="0" skipped="0" failures="0">
  <testcase name="testOne" classname="com.example.FooTest" time="0.01"/>
  <testcase name="testTwo" classname="com.example.FooTest" time="0.02"/>
</testsuite>
"""

_SUREFIRE_FAIL = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.BarTest" tests="2" errors="0" skipped="0" failures="1">
  <testcase name="testOk" classname="com.example.BarTest" time="0.01"/>
  <testcase name="testBroken" classname="com.example.BarTest" time="0.03">
    <failure message="expected:&lt;1&gt; but was:&lt;2&gt;" type="org.opentest4j.AssertionFailedError">stack trace here</failure>
  </testcase>
</testsuite>
"""


def test_parse_surefire_reports_all_passing(tmp_path):
    reports_dir = tmp_path / "target" / "surefire-reports"
    _write(str(reports_dir / "TEST-com.example.FooTest.xml"), _SUREFIRE_PASS)
    result = java_tools._parse_surefire_reports(str(tmp_path), ["com.example.FooTest"])
    assert result["reports_found"] is True
    assert result["passed"] is True
    assert result["error_count"] == 0
    assert result["tests_run"] == 2


def test_parse_surefire_reports_with_failure(tmp_path):
    reports_dir = tmp_path / "target" / "surefire-reports"
    _write(str(reports_dir / "TEST-com.example.BarTest.xml"), _SUREFIRE_FAIL)
    result = java_tools._parse_surefire_reports(str(tmp_path), ["com.example.BarTest"])
    assert result["reports_found"] is True
    assert result["passed"] is False
    assert result["error_count"] == 1
    assert "com.example.BarTest#testBroken" in result["error_lines"][0]


def test_parse_surefire_reports_missing_report(tmp_path):
    result = java_tools._parse_surefire_reports(str(tmp_path), ["com.example.NoSuchTest"])
    assert result["reports_found"] is False


def test_format_maven_test_command_no_planned_targets(tmp_path):
    error, tokens = java_tools._format_maven_test_command(str(tmp_path), ("mvn", "test"), [], [])
    assert error is not None
    assert tokens is None


def test_format_maven_test_command_builds_dtest_flag(tmp_path):
    rel = "src/test/java/com/example/FooTest.java"
    _write(str(tmp_path / rel), "package com.example;\nclass FooTest {}\n")
    error, tokens = java_tools._format_maven_test_command(str(tmp_path), ("mvn", "test"), [], [rel])
    assert error is None
    assert tokens == ["mvn", "test", "-Dtest=com.example.FooTest"]


def test_format_maven_test_command_respects_explicit_dtest(tmp_path):
    error, tokens = java_tools._format_maven_test_command(
        str(tmp_path), ("mvn", "test"), ["-Dtest=com.example.FooTest"], []
    )
    assert error is None
    assert tokens is None  # already scoped by the agent — run as-is


def test_parse_surefire_reports_with_none_reads_every_report(tmp_path):
    """The whole-suite run doesn't know up front which classes executed, so
    it passes None and takes whatever surefire wrote."""
    reports_dir = tmp_path / "target" / "surefire-reports"
    _write(str(reports_dir / "TEST-com.example.FooTest.xml"), _SUREFIRE_PASS)
    _write(str(reports_dir / "TEST-com.example.BarTest.xml"), _SUREFIRE_FAIL)

    result = java_tools._parse_surefire_reports(str(tmp_path), None)

    assert result["reports_found"] is True
    assert result["passed"] is False
    assert result["tests_run"] == 4  # 2 from each report
    assert result["error_count"] == 1


def test_parse_surefire_reports_with_none_and_no_reports_dir(tmp_path):
    result = java_tools._parse_surefire_reports(str(tmp_path), None)
    assert result["reports_found"] is False


def test_scoped_parse_ignores_reports_for_other_classes(tmp_path):
    """A scoped run must not pick up a stale report left by an earlier,
    broader run — otherwise a story gets blamed for a failure it never
    touched."""
    reports_dir = tmp_path / "target" / "surefire-reports"
    _write(str(reports_dir / "TEST-com.example.FooTest.xml"), _SUREFIRE_PASS)
    _write(str(reports_dir / "TEST-com.example.BarTest.xml"), _SUREFIRE_FAIL)

    result = java_tools._parse_surefire_reports(str(tmp_path), ["com.example.FooTest"])

    assert result["passed"] is True
    assert result["tests_run"] == 2
