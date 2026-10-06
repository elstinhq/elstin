"""No database needed. Exercises the LanguageProfile registry against
throwaway temp directories — detection, per-profile target classification,
and the mixed-language case (two marker files in one worktree)."""
from __future__ import annotations

import os
import re

from elstin.tools import language_hooks


def _touch(path: str, content: str = "") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def test_detect_profiles_dart(tmp_path):
    _touch(str(tmp_path / "pubspec.yaml"), "name: demo\n")
    profiles = language_hooks.detect_profiles(str(tmp_path))
    assert [p.key for p in profiles] == ["dart_flutter"]


def test_detect_profiles_java_maven(tmp_path):
    _touch(str(tmp_path / "pom.xml"), "<project></project>")
    profiles = language_hooks.detect_profiles(str(tmp_path))
    assert [p.key for p in profiles] == ["java_maven"]


def test_detect_profiles_java_gradle(tmp_path):
    _touch(str(tmp_path / "build.gradle"), "")
    profiles = language_hooks.detect_profiles(str(tmp_path))
    assert [p.key for p in profiles] == ["java_gradle"]


def test_detect_profiles_vue_vite(tmp_path):
    _touch(str(tmp_path / "vite.config.ts"), "export default {}\n")
    profiles = language_hooks.detect_profiles(str(tmp_path))
    assert [p.key for p in profiles] == ["vue_vite"]


def test_detect_profiles_bare_package_json_is_not_vue(tmp_path):
    """Vite markers gate the Vue profile — a lone package.json must not
    claim JS/Vue files or silently skip-pass analyze/test."""
    _touch(str(tmp_path / "package.json"), "{}")
    assert language_hooks.detect_profiles(str(tmp_path)) == []


def test_detect_profiles_none(tmp_path):
    assert language_hooks.detect_profiles(str(tmp_path)) == []


def test_detect_profiles_mixed_repo(tmp_path):
    """A repo with both a pom.xml and a Vite config at its root — the
    generic case a hardcoded single-language detector couldn't represent."""
    _touch(str(tmp_path / "pom.xml"), "<project></project>")
    _touch(str(tmp_path / "vite.config.ts"), "export default {}\n")
    _touch(str(tmp_path / "package.json"), "{}")
    keys = {p.key for p in language_hooks.detect_profiles(str(tmp_path))}
    assert keys == {"java_maven", "vue_vite"}


def test_classify_targets_java(tmp_path):
    _touch(str(tmp_path / "pom.xml"), "<project></project>")
    paths = {
        "src/main/java/com/example/Foo.java",
        "src/test/java/com/example/FooTest.java",
        "pom.xml",
        "README.md",
    }
    analyze, test = language_hooks.classify_targets(str(tmp_path), paths)
    assert analyze == ["src/main/java/com/example/Foo.java"]
    assert test == ["src/test/java/com/example/FooTest.java"]


def test_classify_targets_dart(tmp_path):
    _touch(str(tmp_path / "pubspec.yaml"), "name: demo\n")
    paths = {
        "lib/widgets/foo.dart",
        "test/widgets/foo_test.dart",
        "integration_test/foo_flow_test.dart",
        "pubspec.yaml",
    }
    analyze, test = language_hooks.classify_targets(str(tmp_path), paths)
    assert analyze == ["lib/widgets/foo.dart"]
    # integration_test/ is excluded from analyze (matches the original
    # hardcoded skip-prefix), but a file ending in _test.dart still counts
    # as a test file regardless of directory — same as the original
    # `endswith("_test.dart") or startswith("test/")` check.
    assert test == ["integration_test/foo_flow_test.dart", "test/widgets/foo_test.dart"]
    assert "integration_test/foo_flow_test.dart" not in analyze


def test_classify_targets_by_profile_mixed_repo(tmp_path):
    _touch(str(tmp_path / "pom.xml"), "<project></project>")
    _touch(str(tmp_path / "vite.config.ts"), "export default {}\n")
    paths = {
        "src/main/java/com/example/Foo.java",
        "src/components/Foo.vue",
        "tests/unit/Foo.spec.ts",
    }
    by_profile = language_hooks.classify_targets_by_profile(str(tmp_path), paths)
    assert by_profile["java_maven"][0] == ["src/main/java/com/example/Foo.java"]
    assert by_profile["vue_vite"] == (
        ["src/components/Foo.vue"],
        ["tests/unit/Foo.spec.ts"],
    )


def test_classify_targets_unmatched_path_is_dropped(tmp_path):
    _touch(str(tmp_path / "pom.xml"), "<project></project>")
    analyze, test = language_hooks.classify_targets(str(tmp_path), {"README.md"})
    assert analyze == []
    assert test == []


def test_run_analyze_no_recognized_project(tmp_path):
    result = language_hooks.run_analyze(str(tmp_path), ["foo.py"])
    assert result == {"passed": True, "skipped": True, "reason": "no_recognized_project_file"}


def test_ensure_dependencies_no_recognized_project(tmp_path):
    result = language_hooks.ensure_dependencies(str(tmp_path))
    assert result == {"passed": True, "skipped": True, "reason": "no_recognized_project_file"}


def test_profile_for_path(tmp_path):
    _touch(str(tmp_path / "pom.xml"), "<project></project>")
    profile = language_hooks.profile_for_path(str(tmp_path), "src/main/java/com/example/Foo.java")
    assert profile is not None
    assert profile.key == "java_maven"
    assert language_hooks.profile_for_path(str(tmp_path), "README.md") is None


def test_run_full_test_suites_reports_not_run_when_unsupported(tmp_path):
    """"Couldn't run the suite" must not be reported as "the suite passed"
    — epic QA prints this verbatim, so conflating them would have the
    report claim verification that never happened."""
    _touch(str(tmp_path / "package.json"), "{}")  # bare Node — no profile
    result = language_hooks.run_full_test_suites(str(tmp_path))
    assert result == {"passed": True, "ran": False, "reason": "no_profile_supports_full_suite", "profiles": {}}


def test_run_full_test_suites_no_recognized_project(tmp_path):
    assert language_hooks.run_full_test_suites(str(tmp_path))["ran"] is False


def test_run_full_test_suites_dispatches_to_every_supporting_profile(tmp_path, monkeypatch):
    """A mixed repo gets each profile's whole suite run, and one failure
    fails the merged result."""
    language_hooks._ensure_default_profiles_registered()
    monkeypatch.setattr(language_hooks, "_PROFILES", dict(language_hooks._PROFILES))

    calls: list[str] = []

    def _profile(key: str, passed: bool):
        def run_all(worktree_path, key=key, passed=passed):
            calls.append(key)
            return {"passed": passed, "tests_run": 3}

        return language_hooks.LanguageProfile(
            key=key, marker_files=(f"{key}.marker",),
            is_source=lambda p: False, is_test=lambda p: False,
            ensure_dependencies=lambda wt: {"passed": True},
            analyze=lambda wt, t: {"passed": True},
            run_tests=lambda wt, t: {"passed": True},
            run_all_tests=run_all,
        )

    language_hooks.register_profile(_profile("alpha", True))
    language_hooks.register_profile(_profile("beta", False))
    _touch(str(tmp_path / "alpha.marker"))
    _touch(str(tmp_path / "beta.marker"))

    result = language_hooks.run_full_test_suites(str(tmp_path))

    assert sorted(calls) == ["alpha", "beta"]
    assert result["ran"] is True
    assert result["passed"] is False  # beta failed
    assert result["profiles"]["alpha"]["passed"] is True


# --- how a developer runs a failing test -------------------------------------


def test_every_profile_with_a_test_command_names_a_way_to_run_it():
    for profile in language_hooks.all_profiles():
        if profile.test_command_prefixes:
            assert language_hooks.test_command_hint(profile.key), profile.key


def test_every_test_command_hint_is_a_command_run_command_tool_allows():
    """The fix brief tells dev to run this. One the allowlist refuses sends it
    back with "blocked by safety policy" — which is how a developer
    lost a round on `./mvnw -q -Dtest=...`."""
    from elstin.tools.safe_shell import is_safe_command

    for profile in language_hooks.all_profiles():
        hint = language_hooks.test_command_hint(profile.key)
        if not hint:
            continue
        tokens = hint.split()
        assert any(tuple(tokens[: len(prefix)]) == prefix for prefix in profile.test_command_prefixes), (profile.key, hint)
        assert is_safe_command(re.sub(r"<[^>]+>", "Example", hint)), (profile.key, hint)


def test_a_profile_without_a_hint_falls_back_to_its_first_test_prefix_and_an_unknown_one_to_nothing():
    language_hooks._ensure_default_profiles_registered()
    plain = language_hooks.LanguageProfile(
        key="plain", marker_files=("plain.marker",), is_source=lambda p: False, is_test=lambda p: False,
        ensure_dependencies=lambda w: {"passed": True}, analyze=lambda w, t: {"passed": True}, run_tests=lambda w, t: {"passed": True},
        test_command_prefixes=(("plain", "test"), ("plain", "check")),
    )
    saved = dict(language_hooks._PROFILES)
    try:
        language_hooks.register_profile(plain)
        assert language_hooks.test_command_hint("plain") == "plain test"
    finally:
        language_hooks._PROFILES.clear()
        language_hooks._PROFILES.update(saved)
    assert language_hooks.test_command_hint("nosuchlang") == ""
