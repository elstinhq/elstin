"""Maven/Gradle (Spring Boot) `compile` + `test` invocation, the Java
analog of `dart_tools.py`. Kept as its own module for the same reason
`dart_tools.py` is — bigger than a one-line hook.

Spring wiring is annotation/component-scan based, so unlike Dart there is
no separate codegen/DI-registration step to run between "install
dependencies" and "compile": a broken `@Autowired`/`@Bean` wire-up already
surfaces as a compile error or a failing `@SpringBootTest`/Spring Modulith
boundary test via `mvn test`. That's why `java_maven_profile()` /
`java_gradle_profile()` below don't set `verify` — the shared default
pipeline in `language_hooks.py` (ensure_dependencies -> analyze ->
run_tests) is the whole gate.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from elstin.tools import jdk, language_hooks
from elstin.tools.language_hooks import LanguageProfile

MAVEN_COMPILE_TIMEOUT_SECONDS = 300
MAVEN_TEST_TIMEOUT_SECONDS = 600
MAVEN_DEPENDENCY_TIMEOUT_SECONDS = 300
GRADLE_TIMEOUT_SECONDS = 600

_JAVAC_ERROR_RE = re.compile(r"\[ERROR\]\s+(\S+\.java):\[(\d+),(\d+)\]\s*(.*)")
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_DTEST_FLAG_RE = re.compile(r"^-Dtest=")
# javac itself failing, not failing to compile the code: Maven's "Fatal error
# compiling" (the compiler crashed — one story's Lombok on a JDK newer than it
# supports), a class file or release the running JDK cannot handle, or no
# usable JDK at all.
_TOOLCHAIN_FAILURE_RES = tuple(re.compile(p) for p in (
    r"Fatal error compiling:?[^\n]*",
    r"Unsupported class file major version \d+",
    r"(?:error: )?invalid (?:target|source) release:?[^\n]*",
    r"release version \d+ not supported",
    r"No compiler is provided in this environment[^\n]*",
    r"JAVA_HOME (?:environment variable )?is not defined correctly[^\n]*",
    r"Could not create the Java Virtual Machine",
))


def _maven_wrapper(worktree_path: str) -> str:
    return "./mvnw" if os.path.isfile(os.path.join(worktree_path, "mvnw")) else "mvn"


def _gradle_wrapper(worktree_path: str) -> str:
    return "./gradlew" if os.path.isfile(os.path.join(worktree_path, "gradlew")) else "gradle"


def is_java_source(path: str) -> bool:
    return path.endswith(".java") and "/src/main/java/" in f"/{path}"


def is_java_test(path: str) -> bool:
    return path.endswith("Test.java") and "/src/test/java/" in f"/{path}"


def fqcn_for_java_file(worktree_path: str, rel_path: str) -> str | None:
    """Fully-qualified class name for a .java file, read from its `package`
    declaration (not guessed from the path — robust to non-standard source
    roots)."""
    abs_path = os.path.join(worktree_path, rel_path)
    try:
        with open(abs_path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    class_name = os.path.splitext(os.path.basename(rel_path))[0]
    match = _PACKAGE_RE.search(content)
    return f"{match.group(1)}.{class_name}" if match else class_name


def ensure_maven_dependencies(worktree_path: str) -> dict:
    wrapper = _maven_wrapper(worktree_path)
    return language_hooks.run([wrapper, "-q", "dependency:resolve"], cwd=worktree_path, timeout=MAVEN_DEPENDENCY_TIMEOUT_SECONDS)


def ensure_gradle_dependencies(worktree_path: str) -> dict:
    wrapper = _gradle_wrapper(worktree_path)
    return language_hooks.run([wrapper, "dependencies", "-q"], cwd=worktree_path, timeout=GRADLE_TIMEOUT_SECONDS)


def _parse_javac_errors(output: str, worktree_path: str) -> tuple[int, list[str]]:
    errors: list[str] = []
    for line in output.splitlines():
        match = _JAVAC_ERROR_RE.match(line.strip())
        if not match:
            continue
        abs_path, lineno, col, message = match.groups()
        try:
            rel_path = os.path.relpath(abs_path, worktree_path)
        except ValueError:
            rel_path = abs_path
        errors.append(f"{rel_path}:{lineno}:{col}: {message}".strip())
    return len(errors), errors[:25]


def run_maven_analyze(worktree_path: str, targets: list[str]) -> dict:
    """`mvn compile` (via the wrapper when present), with javac diagnostics
    parsed into fine-grained error_lines/error_count — matching the shape
    Dart's analyzer already provides. Deliberately not run with `-q`:
    compiler diagnostics need to survive in the captured output."""
    wrapper = _maven_wrapper(worktree_path)
    result = language_hooks.run([wrapper, "compile"], cwd=worktree_path, timeout=MAVEN_COMPILE_TIMEOUT_SECONDS)
    combined = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part)
    error_count, error_lines = _parse_javac_errors(combined, worktree_path)
    return {**result, "passed": result.get("passed", False) and error_count == 0, "error_count": error_count, "error_lines": error_lines}


def run_gradle_analyze(worktree_path: str, targets: list[str]) -> dict:
    """No fine-grained error_lines for Gradle yet — pass/fail + raw output
    is still a real, useful compile gate. (Both services use Maven;
    this exists so a bare `build.gradle`/`build.gradle.kts` project isn't
    left with nothing.)"""
    wrapper = _gradle_wrapper(worktree_path)
    return language_hooks.run([wrapper, "compileJava", "-q"], cwd=worktree_path, timeout=GRADLE_TIMEOUT_SECONDS)


def _parse_surefire_reports(worktree_path: str, classes: list[str] | None) -> dict:
    """`classes=None` means "every report in the directory" — used by the
    whole-suite run, where the set of classes that executed isn't known up
    front. A list means "only these", which keeps a scoped run from
    reporting failures in stale reports left by an earlier run."""
    reports_dir = os.path.join(worktree_path, "target", "surefire-reports")
    total_tests = 0
    total_failures = 0
    error_lines: list[str] = []
    found_any = False
    if classes is None:
        report_paths = sorted(
            os.path.join(reports_dir, name)
            for name in (os.listdir(reports_dir) if os.path.isdir(reports_dir) else [])
            if name.startswith("TEST-") and name.endswith(".xml")
        )
    else:
        report_paths = [os.path.join(reports_dir, f"TEST-{fqcn}.xml") for fqcn in classes]

    for report_path in report_paths:
        fqcn = os.path.basename(report_path)[len("TEST-"):-len(".xml")]
        if not os.path.isfile(report_path):
            continue
        try:
            root = ET.parse(report_path).getroot()
        except ET.ParseError:
            continue
        found_any = True
        total_tests += int(root.get("tests", 0) or 0)
        for testcase in root.findall("testcase"):
            for tag in ("failure", "error"):
                node = testcase.find(tag)
                if node is None:
                    continue
                total_failures += 1
                name = f"{testcase.get('classname', fqcn)}#{testcase.get('name', '?')}"
                message = (node.get("message") or node.text or "").strip().splitlines()[0] if (node.get("message") or node.text) else ""
                error_lines.append(f"{name}: {message}"[:500])
    return {"reports_found": found_any, "passed": total_failures == 0, "error_count": total_failures, "error_lines": error_lines[:25], "tests_run": total_tests}


def run_maven_test(worktree_path: str, test_files: list[str]) -> dict:
    """Maps each planned `*Test.java` path to its fully-qualified class
    name and runs `mvn test -Dtest=Class1,Class2` (wrapper-preferred), then
    parses `target/surefire-reports/*.xml` for pass/fail + failure
    messages — structured JUnit XML instead of scraping stdout."""
    classes = sorted({fqcn for f in test_files if (fqcn := fqcn_for_java_file(worktree_path, f))})
    if not classes:
        return {"passed": True, "skipped": True, "reason": "no_test_classes_resolved"}

    wrapper = _maven_wrapper(worktree_path)
    proc_result = language_hooks.run([wrapper, "-q", "test", f"-Dtest={','.join(classes)}"], cwd=worktree_path, timeout=MAVEN_TEST_TIMEOUT_SECONDS)
    summary = _parse_surefire_reports(worktree_path, classes)
    passed = summary["passed"] if summary["reports_found"] else proc_result.get("passed", False)

    return {
        "passed": passed,
        "exit_code": proc_result.get("exit_code"),
        "stdout": proc_result.get("stdout", ""),
        "stderr": proc_result.get("stderr", ""),
        "timed_out": proc_result.get("timed_out", False),
        "error_count": summary.get("error_count", 0),
        "error_lines": summary.get("error_lines", []),
        "tests_run": summary.get("tests_run", 0),
        "classes": classes,
    }


def run_gradle_test(worktree_path: str, test_files: list[str]) -> dict:
    classes = sorted({fqcn for f in test_files if (fqcn := fqcn_for_java_file(worktree_path, f))})
    if not classes:
        return {"passed": True, "skipped": True, "reason": "no_test_classes_resolved"}
    wrapper = _gradle_wrapper(worktree_path)
    cmd = [wrapper, "test", "-q"]
    for fqcn in classes:
        cmd.extend(["--tests", fqcn])
    return language_hooks.run(cmd, cwd=worktree_path, timeout=GRADLE_TIMEOUT_SECONDS)


def run_maven_all_tests(worktree_path: str) -> dict:
    """Whole-suite run — `mvn test` with no `-Dtest` filter. Deliberately
    not `run_maven_test(worktree_path, [])`, which short-circuits to
    "skipped" because an empty `-Dtest=` selects nothing. Surefire still
    writes its XML reports, so failures come back structured rather than
    scraped from stdout."""
    wrapper = _maven_wrapper(worktree_path)
    proc_result = language_hooks.run([wrapper, "-q", "test"], cwd=worktree_path, timeout=MAVEN_TEST_TIMEOUT_SECONDS)
    summary = _parse_surefire_reports(worktree_path, None)
    passed = summary["passed"] if summary["reports_found"] else proc_result.get("passed", False)
    return {
        "passed": passed,
        "exit_code": proc_result.get("exit_code"),
        "stdout": proc_result.get("stdout", ""),
        "stderr": proc_result.get("stderr", ""),
        "timed_out": proc_result.get("timed_out", False),
        "error_count": summary.get("error_count", 0),
        "error_lines": summary.get("error_lines", []),
        "tests_run": summary.get("tests_run", 0),
    }


def run_gradle_all_tests(worktree_path: str) -> dict:
    wrapper = _gradle_wrapper(worktree_path)
    return language_hooks.run([wrapper, "test", "-q"], cwd=worktree_path, timeout=GRADLE_TIMEOUT_SECONDS)


def _format_maven_test_command(worktree_path: str, prefix: tuple[str, ...], requested_args: list[str], planned_test_targets: list[str]) -> tuple[str | None, list[str] | None]:
    if any(_DTEST_FLAG_RE.match(arg) for arg in requested_args):
        return None, None  # agent already scoped it explicitly — trust it, run as-is
    classes = sorted({fqcn for f in planned_test_targets if (fqcn := fqcn_for_java_file(worktree_path, f))})
    if not classes:
        return "BLOCKED: no planned test classes could be resolved from the approved plan.", None
    return None, [*prefix, f"-Dtest={','.join(classes)}"]


def _format_gradle_test_command(worktree_path: str, prefix: tuple[str, ...], requested_args: list[str], planned_test_targets: list[str]) -> tuple[str | None, list[str] | None]:
    if "--tests" in requested_args:
        return None, None
    classes = sorted({fqcn for f in planned_test_targets if (fqcn := fqcn_for_java_file(worktree_path, f))})
    if not classes:
        return "BLOCKED: no planned test classes could be resolved from the approved plan.", None
    cmd = list(prefix)
    for fqcn in classes:
        cmd.extend(["--tests", fqcn])
    return None, cmd


JAVA_PROMPT_CONVENTIONS = """
Java/Spring Boot conventions for this codebase:
- Layering is `controller -> service (interface + impl/) -> repository -> domain/entities`. Put request handling in controllers, business logic in service impl classes, and data access in repositories — don't collapse layers.
- Use MapStruct (`@Mapper(componentModel = "spring")`) for entity/DTO mapping rather than hand-writing field-by-field mapper code, when a mapper package/pattern already exists in the project.
- Follow the project's existing convention for mutable vs. immutable types: Lombok (`@Data`, `@Builder`) on mutable entities, Java records for immutable value/domain objects — match whichever the surrounding code already uses, don't mix within one class.
- If the project uses Spring Data JDBC (not JPA/Hibernate): there is no lazy loading or dirty checking. Write explicit `@Query` SQL or use `NamedParameterJdbcTemplate` — do not assume JPA cascade/fetch semantics.
- Database migrations (if the project uses Flyway) go in `src/main/resources/db/migration/` following `V{major}_{minor}__description.sql` naming. Never edit an already-applied migration file — add a new one.
- Test classes are named `*Test.java` under `src/test/java/`, mirroring the main source package. Use JUnit 5 (`@Test`, `@SpringBootTest` for context-loading tests) and the project's existing test utilities/builders rather than constructing fixtures by hand when one already exists.
- Spring wiring is annotation/component-scan based (`@Service`, `@Repository`, `@Component`, `@Autowired`/constructor injection) — there is no separate manual DI-registration file to update, unlike some other frameworks. A missing bean surfaces as a startup/context-load failure, not a silent bug.
- If the project uses Spring Modulith module boundaries, do not add a dependency from one module's package to another module's internals (only its public API) — this breaks the module-boundary test, which is a blocking check, not a suggestion.
""".strip()


def java_environment_problem(worktree_path: str, result: dict) -> str | None:
    """Why a failed build is the JDK rather than the code, or None. Only a
    failure with no compiler error in the source qualifies: one of those is
    the code's, whatever else the output says."""
    if result.get("passed", True) or result.get("error_count"):
        return None
    output = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part)
    for pattern in _TOOLCHAIN_FAILURE_RES:
        match = pattern.search(output)
        if match:
            return f"The Java build fails before compiling any source: {match.group(0).strip()}. {jdk.describe(worktree_path)}"
    return None


def java_maven_profile() -> LanguageProfile:
    return LanguageProfile(
        key="java_maven",
        marker_files=("pom.xml",),
        is_source=is_java_source,
        is_test=is_java_test,
        ensure_dependencies=ensure_maven_dependencies,
        analyze=run_maven_analyze,
        run_tests=run_maven_test,
        run_all_tests=run_maven_all_tests,
        safe_command_prefixes=(
            ("mvn", "test"),
            ("mvn", "compile"),
            ("mvn", "-q", "dependency:resolve"),
            ("./mvnw", "test"),
            ("./mvnw", "compile"),
            ("./mvnw", "-q", "dependency:resolve"),
        ),
        analyze_command_prefixes=(("mvn", "compile"), ("./mvnw", "compile")),
        test_command_prefixes=(("mvn", "test"), ("./mvnw", "test")),
        test_command_hint="./mvnw test -Dtest=<TestClass>",
        format_test_command=_format_maven_test_command,
        dev_conventions=JAVA_PROMPT_CONVENTIONS,
        command_env=jdk.java_env,
        build_output=("target/",),
        environment_problem=java_environment_problem,
    )


def java_gradle_profile() -> LanguageProfile:
    return LanguageProfile(
        key="java_gradle",
        marker_files=("build.gradle", "build.gradle.kts"),
        is_source=is_java_source,
        is_test=is_java_test,
        ensure_dependencies=ensure_gradle_dependencies,
        analyze=run_gradle_analyze,
        run_tests=run_gradle_test,
        run_all_tests=run_gradle_all_tests,
        safe_command_prefixes=(
            ("./gradlew", "test"),
            ("./gradlew", "build"),
            ("./gradlew", "compileJava"),
            ("gradle", "test"),
            ("gradle", "build"),
        ),
        analyze_command_prefixes=(("./gradlew", "compileJava"), ("gradle", "compileJava")),
        test_command_prefixes=(("./gradlew", "test"), ("gradle", "test")),
        test_command_hint="./gradlew test --tests <TestClass>",
        format_test_command=_format_gradle_test_command,
        dev_conventions=JAVA_PROMPT_CONVENTIONS,
        command_env=jdk.java_env,
        build_output=("build/", ".gradle/"),
        environment_problem=java_environment_problem,
    )
