"""Executable versions of AGENTS.md's "Hard constraints (do not violate)".

Those constraints are the reason this codebase stays readable — layering
that's only written down gets violated by the next person in a hurry (or
by an agent working on this repo, which is the whole point of the
product). Each test below is one constraint, checked by walking the real
import graph with `ast`, so a violation fails CI instead of surviving to
review.

Deliberately import-level and cheap: no DB, no network, no provider.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

ELSTIN_ROOT = Path(__file__).resolve().parent.parent / "elstin"


def _python_files(*, under: str | None = None) -> list[Path]:
    root = ELSTIN_ROOT / under if under else ELSTIN_ROOT
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_modules(path: Path) -> set[str]:
    """Every dotted module name `path` imports, top-level names included
    (so both `import openai` and `from openai import X` are caught)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _relative(path: Path) -> str:
    return os.path.relpath(path, ELSTIN_ROOT.parent)


# Provider SDKs. Anything here may only be imported from inside
# elstin/llm/, and in practice only by a single adapter module.
_PROVIDER_SDKS = ("openai", "anthropic", "claude_agent_sdk", "google", "cohere", "mistralai", "litellm")


def test_only_the_adapter_imports_a_provider_sdk():
    """AGENTS.md hard constraints #9 and #11: elstin/llm/ is the only
    package that may import a provider SDK, and one adapter module inside
    it is the only place that may import `openai`. This is what makes
    "swap the provider by writing one file" true rather than aspirational
    — a stray `import openai` in a role would silently re-couple every
    caller to OpenAI's shapes."""
    allowed = {"elstin/llm/openai_compat.py"}
    offenders: list[str] = []
    for path in _python_files():
        rel = _relative(path)
        if rel in allowed:
            continue
        for module in _imported_modules(path):
            if module.split(".")[0] in _PROVIDER_SDKS:
                offenders.append(f"{rel} imports {module}")
    assert offenders == [], (
        "Provider SDKs may only be imported by the adapter module(s) in elstin/llm/. "
        f"Offenders: {offenders}. Add a new adapter module beside openai_compat.py "
        "(and list it in this test's `allowed` set) rather than importing a provider elsewhere."
    )


def test_db_models_are_only_imported_by_state_and_db_layers():
    """AGENTS.md hard constraint #1: never import elstin.db.models outside
    elstin/db/ and elstin/state/. services/run_activity.py + usage.py are
    the two documented exceptions (run telemetry isn't epic/story state —
    see their module docstrings), and they are pinned here so the
    exception stays a short, deliberate list instead of quietly growing."""
    documented_exceptions = {
        # The config store is to app_config/project_config what
        # StateManager is to epics/stories — the single owner of those
        # tables, not a bypass of one.
        "elstin/config/store.py",
        # Run telemetry: not epic/story state, documented in each module.
        "elstin/services/run_activity.py",
        "elstin/services/usage.py",
        "elstin/services/activity_stream.py",
    }
    offenders: list[str] = []
    for path in _python_files():
        rel = _relative(path)
        if rel.startswith(("elstin/db/", "elstin/state/")) or rel in documented_exceptions:
            continue
        for module in _imported_modules(path):
            if module.startswith("elstin.db.models"):
                offenders.append(f"{rel} imports {module}")
    assert offenders == [], (
        "Go through StateManager (elstin/state/) instead of importing elstin.db.models directly. "
        f"Offenders: {offenders}"
    )


def test_reconciler_stays_pure():
    """AGENTS.md hard constraint #2: the reconciler decides *what to do
    next* and nothing else — no DB, no network, no filesystem, no LLM. It
    is the one module in the system that can be reasoned about by reading
    it, and that property only survives if nothing I/O-shaped sneaks in."""
    forbidden_prefixes = ("elstin.db", "elstin.state", "elstin.llm", "elstin.agents", "elstin.services", "openai", "httpx", "subprocess", "requests")
    imported = _imported_modules(ELSTIN_ROOT / "orchestration" / "reconciler.py")
    offenders = sorted(m for m in imported if m.startswith(forbidden_prefixes))
    assert offenders == [], f"elstin/orchestration/reconciler.py must stay pure; found: {offenders}"


def test_roles_do_not_construct_a_provider_directly():
    """AGENTS.md hard constraint #9, second half: a role goes through
    elstin/agents/loop.py (dev/planner) or elstin/services/generation.py
    (verifier/qa). Reaching for a concrete adapter class would bypass
    elstin/llm/registry.py's config resolution, so a /settings change
    would silently stop applying to that role."""
    offenders: list[str] = []
    for path in _python_files(under="agents/roles"):
        source = path.read_text(encoding="utf-8")
        if "OpenAiCompatProvider" in source:
            offenders.append(_relative(path))
    assert offenders == [], (
        f"Roles must resolve a provider via elstin/llm/registry.py:get_provider(), not name an adapter: {offenders}"
    )


def test_tools_do_not_branch_on_a_language_name():
    """AGENTS.md's tools rule (and CLAUDE.md's "never special-case a
    language name inside safe_shell/dev_tools directly"): a language
    belongs in its own `tools/<lang>_tools.py` profile registered through
    `language_hooks`, never in a conditional in the shared tool layer.

    Checks *branch conditions* specifically, via the AST. A tool's
    description string legitimately names example commands ("e.g. 'dart
    run build_runner build'") — that's prompt text the model reads, not
    behavior — so a line-level grep here would cry wolf on it and then
    get switched off, which is worse than not having the check."""
    shared_modules = ["tools/safe_shell.py", "tools/dev_tools.py", "tools/path_guard.py", "tools/edit_file.py"]
    language_names = ("dart", "flutter", "java", "maven", "vue", "npm")
    offenders: list[str] = []
    for rel_module in shared_modules:
        path = ELSTIN_ROOT / rel_module
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            tests = []
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                tests = [node.test]
            elif isinstance(node, ast.Compare):
                tests = [node]
            for test in tests:
                for inner in ast.walk(test):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        lowered = inner.value.lower()
                        if any(name in lowered for name in language_names):
                            offenders.append(f"elstin/{rel_module}:{getattr(inner, 'lineno', '?')}: branches on {inner.value!r}")
    assert offenders == [], (
        "Language-specific behavior belongs in a tools/<lang>_tools.py profile registered via "
        f"language_hooks, not in a conditional in the shared tool layer. Offenders: {offenders}"
    )


def test_tools_that_run_repo_code_always_pass_an_environment():
    """AGENTS.md hard constraint #12: a command that executes code from a
    worktree (a build, a test run, a language server) gets its environment
    from `language_hooks.profile_command_env` — never the daemon's own by
    omission. A bare `subprocess.run(cmd, cwd=...)` inherits `DATABASE_URL`
    and every token in the shell elstin was started from, which is exactly what
    elstin/tools/agent_env.py exists to keep away from code the agent wrote.

    Checked per call, via the AST: every `subprocess.run`/`Popen` in the tool
    modules that run a toolchain must name `env=`. (git, ripgrep and grep
    calls elsewhere execute no repo code and are left alone.)"""
    toolchain_modules = [
        "tools/dart_tools.py", "tools/java_tools.py", "tools/vue_tools.py",
        "tools/lsp_client.py", "tools/safe_shell.py", "tools/language_hooks.py",
    ]
    offenders: list[str] = []
    for rel_module in toolchain_modules:
        path = ELSTIN_ROOT / rel_module
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # process_monitor.run is subprocess.run that also measures memory
            # (elstin/tools/process_monitor.py): the same rule applies to it.
            spawners = {
                "subprocess": ("run", "Popen", "check_output", "check_call"),
                "process_monitor": ("run",),
            }
            is_spawn = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.attr in spawners.get(func.value.id, ())
            )
            if is_spawn and not any(kw.arg == "env" for kw in node.keywords):
                offenders.append(f"elstin/{rel_module}:{node.lineno}: {func.value.id}.{func.attr} without env=")
    assert offenders == [], (
        "Commands that run repo code must pass env=language_hooks.profile_command_env(<worktree>). "
        f"Offenders: {offenders}"
    )


_STORAGE_LITERALS = {"/data"} | {f"/data/{name}" for name in ("repos", "cloned-repos", "jira-attachments", "baselines")}


def test_nothing_hard_codes_a_storage_path():
    """Where worktrees, cloned repos, baselines and attachments live follows the
    database (elstin/config/store.py's `storage_dir`): `/data/...` in the Docker
    image, the user's Application Support folder in the Mac app. A literal path in
    some module would send that module's files somewhere the settings never say."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in _STORAGE_LITERALS:
                offenders.append(f"{_relative(path)}:{node.lineno}: {node.value!r}")

    assert offenders == [], "use storage_dir(<setting>) instead:\n" + "\n".join(offenders)
