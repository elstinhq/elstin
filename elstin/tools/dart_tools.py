"""Dart/Flutter `analyze` + `build_runner` invocation, and the
`LanguageProfile` that wires it all into elstin/tools/language_hooks.py's
registry. Timeouts/binaries/flags are plain module constants (PATH-resolved
tool binaries and tuning knobs, not credentials — matches
elstin/tools/language_hooks.py's existing convention). Kept as its own
module (rather than folded into language_hooks.py) because it's
substantially larger than the other per-language profiles there.

Dart is the one profile with a bespoke `verify` pipeline (pub_get ->
build_runner -> DI-registration check -> analyze -> test, with generated-
file staging in between) instead of the shared default in
language_hooks.py — every other language's real gate is simple enough
that the shared default (ensure_dependencies -> analyze -> run_tests)
covers it."""
import hashlib
import os
import re
import subprocess
from typing import Any

from elstin.tools import process_monitor
from elstin.tools.language_hooks import LanguageProfile, VerifyContext, profile_command_env

DART_BIN = "dart"
FLUTTER_BIN = "flutter"
PUB_GET_TIMEOUT_SECONDS = 180
BUILD_RUNNER_TIMEOUT_SECONDS = 300
DART_ANALYZE_TIMEOUT_SECONDS = 120
RUN_BUILD_RUNNER = True
ANALYZE_BLOCKING = True

_DART_METHOD_RE = re.compile(
    r"^\s+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:async\s+)?(Future<[^>]+>|Future|void|int|bool|String|double|List<[\w<>, ]+>|\w+)\s+"
    r"(\w+)\s*\(",
    re.MULTILINE,
)

_DART_EXPR_LEAD_TOKENS = frozenset(
    {
        "return", "await", "yield", "throw", "const", "new", "final", "var",
        "else", "case", "if", "for", "while", "switch", "catch", "assert",
        "super", "this",
    }
)

_GENERATED_SUFFIXES = (".g.dart", ".freezed.dart", ".gr.dart", ".mocks.dart")


def _read_pubspec(worktree_path: str) -> str | None:
    pubspec = os.path.join(worktree_path, "pubspec.yaml")
    if not os.path.isfile(pubspec):
        return None
    try:
        return open(pubspec, encoding="utf-8").read()
    except OSError:
        return None


def is_flutter_project(worktree_path: str) -> bool:
    content = _read_pubspec(worktree_path)
    if not content:
        return False
    return "flutter:" in content


def project_has_build_runner(worktree_path: str) -> bool:
    content = _read_pubspec(worktree_path)
    if not content:
        return False
    return "build_runner" in content


def needs_pub_get(worktree_path: str) -> bool:
    pubspec = os.path.join(worktree_path, "pubspec.yaml")
    package_config = os.path.join(worktree_path, ".dart_tool", "package_config.json")
    if not os.path.isfile(pubspec):
        return False
    if not os.path.isfile(package_config):
        return True
    return os.path.getmtime(pubspec) > os.path.getmtime(package_config)


def ensure_pub_dependencies(worktree_path: str) -> dict:
    """Install Dart/Flutter package dependencies in the worktree when needed."""
    if not needs_pub_get(worktree_path):
        return {"passed": True, "skipped": True}

    timeout = PUB_GET_TIMEOUT_SECONDS
    cmd = [FLUTTER_BIN, "pub", "get"] if is_flutter_project(worktree_path) else [DART_BIN, "pub", "get"]

    try:
        proc = process_monitor.run(cmd, cwd=worktree_path, timeout=timeout, env=profile_command_env(worktree_path))
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", errors="replace") if exc.stdout else ""
        err = (exc.stderr or b"").decode("utf-8", errors="replace") if exc.stderr else ""
        return {
            "passed": False, "exit_code": -1, "stdout": output[-8000:],
            "stderr": f"pub get timed out after {timeout}s\n{err}"[-8000:],
            "timed_out": True, "skipped": False, "command": " ".join(cmd),
        }

    return {
        "passed": proc.returncode == 0, "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:] if proc.stdout else "",
        "stderr": proc.stderr[-8000:] if proc.stderr else "",
        "timed_out": False, "skipped": False, "command": " ".join(cmd),
    }


def should_run_build_runner(worktree_path: str, dart_paths: list[str]) -> bool:
    if not RUN_BUILD_RUNNER:
        return False
    if not dart_paths:
        return False
    return project_has_build_runner(worktree_path)


def run_build_runner(worktree_path: str) -> dict:
    """Run `dart run build_runner build --delete-conflicting-outputs` in the worktree."""
    pub_get = ensure_pub_dependencies(worktree_path)
    if not pub_get.get("passed", True):
        pub_get["skipped"] = False
        return pub_get

    timeout = BUILD_RUNNER_TIMEOUT_SECONDS
    cmd = [DART_BIN, "run", "build_runner", "build", "--delete-conflicting-outputs"]

    try:
        proc = process_monitor.run(cmd, cwd=worktree_path, timeout=timeout, env=profile_command_env(worktree_path))
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", errors="replace") if exc.stdout else ""
        err = (exc.stderr or b"").decode("utf-8", errors="replace") if exc.stderr else ""
        return {
            "passed": False, "exit_code": -1, "stdout": output[-8000:],
            "stderr": f"build_runner timed out after {timeout}s\n{err}"[-8000:],
            "timed_out": True, "skipped": False,
        }

    return {
        "passed": proc.returncode == 0, "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:] if proc.stdout else "",
        "stderr": proc.stderr[-8000:] if proc.stderr else "",
        "timed_out": False, "skipped": False,
    }


def maybe_run_build_runner(worktree_path: str, dart_paths: list[str]) -> dict:
    """Run build_runner when the project uses it and Dart files were changed."""
    if not should_run_build_runner(worktree_path, dart_paths):
        return {"passed": True, "skipped": True, "targets": dart_paths}
    result = run_build_runner(worktree_path)
    result["targets"] = dart_paths
    return result


def dart_source_signature(worktree_path: str, dart_paths: list[str]) -> str:
    """Content hash of the non-generated Dart source files in *dart_paths* —
    lets the verifier skip re-running build_runner when nothing changed."""
    digest = hashlib.sha256()
    for rel in sorted(set(dart_paths)):
        if not rel.endswith(".dart") or is_generated_dart_path(rel):
            continue
        abs_path = os.path.join(worktree_path, rel)
        try:
            with open(abs_path, "rb") as handle:
                data = handle.read()
        except OSError:
            data = b""
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def is_generated_dart_path(path: str) -> bool:
    return path.endswith(_GENERATED_SUFFIXES)


_INJECTABLE_CLASS_RE = re.compile(
    r"@(?:lazySingleton|singleton|injectable|Injectable|LazySingleton|Singleton)\b"
    r"[^\n]*\n(?:\s*@[^\n]*\n)*"
    r"\s*(?:abstract\s+)?class\s+(\w+)",
)


def injectable_classes(content: str) -> set[str]:
    return set(_INJECTABLE_CLASS_RE.findall(content))


def check_di_registrations(worktree_path: str, created_paths: list[str]) -> dict:
    """Confirm newly-created injectable classes are wired into the generated
    DI graph. injectable/get_it registrations live in generated
    *.config.dart files, which are commonly gitignored (so they never
    appear in a git diff) — this reads those files directly rather than
    trusting the diff.

    Returns {checked, registered: {class_name: config_file}, missing: [class_name]}.
    """
    annotated: dict[str, str] = {}
    for rel in created_paths:
        if not rel.endswith(".dart") or is_generated_dart_path(rel):
            continue
        abs_path = os.path.join(worktree_path, rel)
        try:
            with open(abs_path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            continue
        for cls in injectable_classes(content):
            annotated[cls] = rel

    if not annotated:
        return {"checked": False, "registered": {}, "missing": []}

    config_blobs: list[tuple[str, str]] = []
    lib_root = os.path.join(worktree_path, "lib")
    for root, dirs, files in os.walk(lib_root):
        dirs[:] = [d for d in dirs if d != ".dart_tool"]
        for name in files:
            if name.endswith(".config.dart"):
                abs_cfg = os.path.join(root, name)
                try:
                    with open(abs_cfg, encoding="utf-8") as handle:
                        config_blobs.append((os.path.relpath(abs_cfg, worktree_path), handle.read()))
                except OSError:
                    pass

    registered: dict[str, str] = {}
    for cls in annotated:
        pattern = re.compile(rf"\b{re.escape(cls)}\b")
        for rel_cfg, blob in config_blobs:
            if pattern.search(blob):
                registered[cls] = rel_cfg
                break

    missing = sorted(cls for cls in annotated if cls not in registered)
    return {"checked": True, "annotated": annotated, "registered": registered, "missing": missing}


def run_dart_analyze(worktree_path: str, files: list[str] | None = None) -> dict:
    """Analyze only the provided relative paths when possible (avoids full-project hangs)."""
    pub_get = ensure_pub_dependencies(worktree_path)
    if not pub_get.get("passed", True):
        pub_get["skipped"] = False
        return pub_get

    targets = files or []
    timeout = DART_ANALYZE_TIMEOUT_SECONDS
    cmd = [FLUTTER_BIN, "analyze"] if is_flutter_project(worktree_path) else [DART_BIN, "analyze"]
    if targets:
        cmd.extend(targets)

    try:
        proc = process_monitor.run(cmd, cwd=worktree_path, timeout=timeout, env=profile_command_env(worktree_path))
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", errors="replace") if exc.stdout else ""
        err = (exc.stderr or b"").decode("utf-8", errors="replace") if exc.stderr else ""
        label = cmd[0]
        return {
            "passed": False, "blocking_passed": False, "exit_code": -1, "stdout": output[-8000:],
            "stderr": f"{label} analyze timed out after {timeout}s\n{err}"[-8000:],
            "timed_out": True, "error_count": -1,
        }

    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    error_count, error_lines = _parse_analyze_errors(combined, targets)
    clean_pass = error_count == 0
    blocking_passed = clean_pass or not ANALYZE_BLOCKING

    return {
        "passed": clean_pass, "blocking_passed": blocking_passed,
        "advisory_only": not ANALYZE_BLOCKING and not clean_pass,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:] if proc.stdout else "",
        "stderr": proc.stderr[-8000:] if proc.stderr else "",
        "timed_out": False, "error_count": error_count, "error_lines": error_lines[:25],
    }


def _parse_analyze_errors(output: str, targets: list[str]) -> tuple[int, list[str]]:
    if not output.strip():
        return 0, []
    if not targets:
        errors = [line.strip() for line in output.splitlines() if " error " in line]
        return len(errors), errors[:25]

    errors: list[str] = []
    for line in output.splitlines():
        if " error " not in line:
            continue
        if any(target in line for target in targets):
            errors.append(line.strip())
    return len(errors), errors[:25]


def dart_public_method_names(content: str) -> set[str]:
    """Return method names declared in a Dart class body (loose regex,
    filtered to exclude constructor/widget invocations and expression
    statements)."""
    names: set[str] = set()
    for type_tok, name in _DART_METHOD_RE.findall(content):
        if name[:1].isupper():
            continue
        if type_tok in _DART_EXPR_LEAD_TOKENS:
            continue
        names.add(name)
    return names


def run_flutter_test(worktree_path: str, targets: list[str] | None = None) -> dict:
    """Run unit/widget tests in the worktree."""
    pub_get = ensure_pub_dependencies(worktree_path)
    if not pub_get.get("passed", True):
        pub_get["skipped"] = False
        return pub_get

    timeout = 180
    cmd = [FLUTTER_BIN, "test"] if is_flutter_project(worktree_path) else [DART_BIN, "test"]
    if targets:
        cmd.extend(targets)

    try:
        proc = process_monitor.run(cmd, cwd=worktree_path, timeout=timeout, env=profile_command_env(worktree_path))
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", errors="replace") if exc.stdout else ""
        err = (exc.stderr or b"").decode("utf-8", errors="replace") if exc.stderr else ""
        return {
            "passed": False, "exit_code": -1, "stdout": output[-8000:],
            "stderr": f"test run timed out after {timeout}s\n{err}"[-8000:],
            "timed_out": True, "skipped": False,
        }

    return {
        "passed": proc.returncode == 0, "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:] if proc.stdout else "",
        "stderr": proc.stderr[-8000:] if proc.stderr else "",
        "timed_out": False, "skipped": False,
    }


_TEST_SKIP_PREFIXES = ("test/", "integration_test/")


def is_dart_source(path: str) -> bool:
    return path.endswith(".dart") and path.startswith("lib/") and not path.startswith(_TEST_SKIP_PREFIXES)


def is_dart_test(path: str) -> bool:
    return path.endswith("_test.dart") or path.startswith("test/")


def _dart_check_modified_file(rel: str, original: str, current: str) -> str | None:
    removed = dart_public_method_names(original) - dart_public_method_names(current)
    if not removed:
        return None
    return f"Removed existing methods from {rel}: {', '.join(sorted(removed))}"


def dart_after_dev_changes(worktree_path: str, changed_files: list[str]) -> dict:
    """Runs after the dev loop finishes editing — Dart's codegen step.
    Stages any *.g.dart/*.freezed.dart/etc. build_runner produces, and
    returns a signature over the non-generated .dart inputs so the
    verifier can skip re-running build_runner when nothing changed since."""
    from elstin.tools.git_tools import list_changed_files, stage_files

    dart_paths = [p for p in changed_files if p.endswith(".dart")]
    build_runner = maybe_run_build_runner(worktree_path, dart_paths)
    if build_runner.get("passed") and not build_runner.get("skipped"):
        generated = [p for p in list_changed_files(worktree_path) if is_generated_dart_path(p)]
        if generated:
            stage_files(worktree_path, generated)
    build_runner["signature"] = dart_source_signature(worktree_path, dart_paths)
    return build_runner


def _dart_verify(ctx: VerifyContext) -> dict:
    from elstin.services.run_activity import append_activity
    from elstin.tools.git_tools import list_changed_files, stage_files

    issues: list[dict[str, Any]] = []
    required_fixes: list[str] = []
    dart_targets = ctx.analyze_targets
    pub_get: dict[str, Any] = {"passed": True, "skipped": True}
    build_runner: dict[str, Any] = {"passed": True, "skipped": True, "targets": dart_targets}

    if dart_targets:
        pub_get = ensure_pub_dependencies(ctx.worktree_path)
        if ctx.run_id:
            append_activity(ctx.run_id, type="tool", phase="verifier", title="pub get" if pub_get.get("passed", True) else "pub get failed", meta={"tool": "pub_get", "passed": pub_get.get("passed", True)})
        if not pub_get.get("passed", True):
            msg = "pub get failed"
            issues.append({"severity": "blocker", "file": None, "reason": msg, "details": pub_get.get("stdout") or pub_get.get("stderr")})
            required_fixes.append(msg)
        else:
            prior = ctx.prior_state if isinstance(ctx.prior_state, dict) else {}
            reuse_signature = prior.get("signature", "") if prior.get("passed", False) else ""
            current_signature = dart_source_signature(ctx.worktree_path, [p for p in ctx.changed if p.endswith(".dart")]) if reuse_signature else ""
            if reuse_signature and reuse_signature == current_signature:
                build_runner = {"passed": True, "skipped": True, "reused": True, "targets": dart_targets}
                if ctx.run_id:
                    append_activity(ctx.run_id, type="tool", phase="verifier", title="build_runner reused (unchanged since dev)", meta={"tool": "build_runner", "passed": True, "reused": True})
            else:
                build_runner = maybe_run_build_runner(ctx.worktree_path, dart_targets)
                if ctx.run_id and not build_runner.get("skipped"):
                    append_activity(ctx.run_id, type="tool", phase="verifier", title="build_runner" if build_runner.get("passed") else "build_runner failed", files=dart_targets, meta={"tool": "build_runner", "passed": build_runner.get("passed")})
                if build_runner.get("passed") and not build_runner.get("skipped"):
                    generated = [p for p in list_changed_files(ctx.worktree_path) if is_generated_dart_path(p)]
                    if generated:
                        stage_files(ctx.worktree_path, generated)
                        ctx.changed = set(list_changed_files(ctx.worktree_path)) | ctx.dev_targets
                        dart_targets = sorted(p for p in ctx.changed if is_dart_source(p) and os.path.isfile(os.path.join(ctx.worktree_path, p)))
            if not build_runner.get("passed"):
                msg = "build_runner failed"
                issues.append({"severity": "blocker", "file": None, "reason": msg, "details": build_runner.get("stdout") or build_runner.get("stderr")})
                required_fixes.append(msg)

    created_paths = [
        (item.get("path") if isinstance(item, dict) else str(item))
        for item in (ctx.plan.get("files_to_create") or [])
    ]
    created_paths = [p for p in created_paths if p]
    di_registrations = check_di_registrations(ctx.worktree_path, created_paths)
    if di_registrations.get("checked") and build_runner.get("passed", True):
        if ctx.run_id:
            append_activity(
                ctx.run_id, type="tool", phase="verifier",
                title="DI registration verified" if not di_registrations.get("missing") else "DI registration missing",
                files=sorted(di_registrations.get("registered", {}).values()),
                meta={"tool": "di_check", "registered": list(di_registrations.get("registered", {})), "missing": di_registrations.get("missing", [])},
            )
        for cls in di_registrations.get("missing", []):
            msg = f"{cls} is annotated injectable but is not registered in any generated *.config.dart — run build_runner so it joins the runtime DI graph"
            issues.append({"severity": "blocker", "file": None, "reason": msg})
            required_fixes.append(msg)

    analyze: dict[str, Any] = {"passed": True, "skipped": True, "targets": dart_targets}
    if dart_targets and pub_get.get("passed", True) and build_runner.get("passed", True):
        analyze = run_dart_analyze(ctx.worktree_path, dart_targets)
        analyze["targets"] = dart_targets
        if ctx.run_id:
            append_activity(ctx.run_id, type="tool", phase="verifier", title="dart analyze" if analyze.get("passed", True) else "dart analyze failed", files=dart_targets, meta={"tool": "dart_analyze", "passed": analyze.get("passed", True), "error_count": analyze.get("error_count")})
        blocking_passed = analyze.get("blocking_passed", analyze.get("passed", True))
        if not blocking_passed:
            msg = "dart analyze failed on changed lib files"
            details = "\n".join(analyze.get("error_lines") or []) or analyze.get("stdout") or analyze.get("stderr")
            issues.append({"severity": "blocker", "file": None, "reason": msg, "details": details})
            required_fixes.append(msg)
        elif not analyze.get("passed", True):
            issues.append({"severity": "warning", "file": None, "reason": "dart analyze reported errors in changed lib files", "details": "\n".join(analyze.get("error_lines") or [])[:4000]})

    test_results: dict[str, Any] = {"passed": True, "skipped": True}
    if ctx.test_targets and pub_get.get("passed", True) and build_runner.get("passed", True):
        if ctx.run_id:
            append_activity(ctx.run_id, type="status", phase="verifier", title=f"Running {len(ctx.test_targets)} changed test(s)")
        test_results = run_flutter_test(ctx.worktree_path, ctx.test_targets)
        if ctx.run_id:
            append_activity(ctx.run_id, type="tool", phase="verifier", title="flutter test" if test_results.get("passed", True) else "flutter test failed", files=ctx.test_targets, meta={"tool": "flutter_test", "passed": test_results.get("passed", True)})
        if not test_results.get("passed", True):
            msg = "Automated unit tests failed"
            details = (test_results.get("stdout", "") + "\n" + test_results.get("stderr", ""))[-4000:]
            issues.append({"severity": "blocker", "file": None, "reason": msg, "details": details})
            required_fixes.append(msg)

    return {
        "passed": len(required_fixes) == 0,
        "issues": issues,
        "required_fixes": required_fixes,
        "report": {"pub_get": pub_get, "build_runner": build_runner, "analyze": analyze, "test_results": test_results, "di_registrations": di_registrations},
        "state": None,
    }


_DEPRECATED_API_CONVENTIONS = """
Deprecated API migration:
- When the analyzer warns about a deprecated API (e.g., withOpacity), you MUST call the lookup_sdk_symbol tool with the REPLACEMENT method name BEFORE writing any fix.
- Example: if "withOpacity" is deprecated, call lookup_sdk_symbol(query="withValues") to learn the correct signature.
- Do NOT guess the replacement API's parameters. The SDK signature is the only source of truth.
- Common trap: Color.withValues() uses NAMED parameter `alpha`, e.g. `Colors.white.withValues(alpha: 0.6)`.

Dart constructor patterns:
- Modern Dart uses `super.key` in the parameter list. Do NOT also add `: super(key: key)` in the initializer — that causes a duplicate argument error.
- Correct: `const MyWidget({super.key, required this.child});`
- Wrong:  `const MyWidget({super.key, required this.child}) : super(key: key);`
- A const constructor must NOT have a body `{}`. End with `;`.

If compile errors OR verifier feedback relate to an unknown API, wrong arguments, or deprecated methods, use the lookup_sdk_symbol tool to check the correct signature BEFORE attempting a fix. Do NOT guess.
""".strip()

_DI_REGISTRATION_CONVENTIONS = """
CRITICAL — dependency injection registration:
- Whenever files_to_create contains a class annotated with @injectable, @lazySingleton, @singleton, or any GetIt-registered type, the DI registration file (typically lib/core/di/dependency_injector.dart or equivalent found in AGENTS.md) MUST be in files_to_modify.
- This applies even when the class is self-registering via build_runner codegen — the manual registration call must still be added to the DI setup file.
- If you are unsure which file registers dependencies, search the discovery context for "di.register" or "GetIt" and use that file.
""".strip()

_VERIFIER_REVIEW_CONVENTIONS = """
IMPORTANT — generated code is invisible in diffs. Files matching
*.config.dart, *.g.dart, *.freezed.dart, *.mocks.dart are build_runner
output and are gitignored, so they will NOT appear in the diffs you are
shown. Dependency-injection registrations (injectable/get_it) live in
*.config.dart. Do NOT flag a service as 'not registered', 'missing DI
wiring', or 'not resolvable through the app DI path' based on its absence
from the diff. The field deterministic_gate.profiles.dart_flutter.di_registrations
is the source of truth: every class listed under its 'registered' map is
confirmed wired into the runtime DI graph. Only raise a DI blocker for a
class that appears in di_registrations.missing. A class carrying
@injectable/@lazySingleton/@singleton needs no manual registration call —
build_runner generates it.
""".strip()


def _norm_rel(path: str) -> str:
    return os.path.normpath(str(path).strip().lstrip("/")).replace("\\", "/")


def _dart_format_test_command(worktree_path: str, prefix: tuple[str, ...], requested_args: list[str], planned_test_targets: list[str]) -> tuple[str | None, list[str] | None]:
    requested = [tok for tok in requested_args if not tok.startswith("-")]
    if requested:
        invalid = [rel for rel in (_norm_rel(tok) for tok in requested) if rel not in planned_test_targets]
        if invalid:
            return f"BLOCKED: flutter test may only target planned test files. Not in plan: {', '.join(invalid)}", None
        return None, None
    if planned_test_targets:
        return None, [*prefix, *planned_test_targets]
    return None, None


def dart_flutter_profile() -> LanguageProfile:
    return LanguageProfile(
        key="dart_flutter",
        marker_files=("pubspec.yaml",),
        is_source=is_dart_source,
        is_test=is_dart_test,
        ensure_dependencies=ensure_pub_dependencies,
        analyze=run_dart_analyze,
        run_tests=run_flutter_test,
        # `flutter test` with no target paths already means "the whole
        # suite", so the full-suite hook is the same call with no targets.
        run_all_tests=lambda worktree_path: run_flutter_test(worktree_path, []),
        verify=_dart_verify,
        check_modified_file=_dart_check_modified_file,
        after_dev_changes=dart_after_dev_changes,
        safe_command_prefixes=(
            ("flutter", "pub", "get"),
            ("flutter", "pub", "run"),
            ("flutter", "test"),
            ("flutter", "analyze"),
            ("dart", "pub", "get"),
            ("dart", "analyze"),
            ("dart", "run", "build_runner"),
        ),
        analyze_command_prefixes=(("flutter", "analyze"), ("dart", "analyze")),
        test_command_prefixes=(("flutter", "test"), ("dart", "test")),
        test_command_hint="flutter test <path/to/the_test.dart>",
        format_test_command=_dart_format_test_command,
        planner_conventions=_DI_REGISTRATION_CONVENTIONS,
        dev_conventions=_DEPRECATED_API_CONVENTIONS,
        verifier_review_conventions=_VERIFIER_REVIEW_CONVENTIONS,
        build_output=(".dart_tool/", "build/"),
    )
