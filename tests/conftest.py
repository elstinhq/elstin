from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _never_the_users_database(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """With no DATABASE_URL, elstin opens the file in the user's own data
    directory — the real one, if the suite runs on a developer's machine. Point
    that default at a directory nothing has made, and fail any test that makes
    it by opening the database without choosing one (use `elstin_db`)."""
    import elstin.db.engine as engine_mod

    unused = tmp_path_factory.mktemp("elstin-default-home") / "must-stay-absent"
    monkeypatch.setattr(engine_mod, "data_home", lambda: unused)
    # No engine carried over from an earlier test: a cached one would open
    # nothing and hide that this test never chose a database.
    engine_mod._engine = None
    engine_mod._SessionLocal = None
    yield
    if engine_mod._engine is not None:
        engine_mod._engine.dispose()
    engine_mod._engine = None
    engine_mod._SessionLocal = None
    assert not unused.exists(), (
        "this test opened elstin's default database (no DATABASE_URL set); "
        "take the `elstin_db` fixture so it gets a database of its own"
    )


def _run_git(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path):
    """A repo with a real `origin` remote (a bare repo alongside it) — every
    real target repo has one, and worktree.py's `origin/<default>` fallback
    assumes it, so the fixture should too rather than the code special-casing
    an origin-less repo that never occurs in practice."""
    bare_path = tmp_path / "widgets.git"
    _run_git(["git", "init", "--bare", "-b", "main", str(bare_path)], str(tmp_path))

    repo_path = tmp_path / "widgets"
    _run_git(["git", "clone", str(bare_path), str(repo_path)], str(tmp_path))
    _run_git(["git", "config", "user.email", "test@example.com"], str(repo_path))
    _run_git(["git", "config", "user.name", "Test"], str(repo_path))
    (repo_path / "README.md").write_text("hello\n")
    _run_git(["git", "add", "README.md"], str(repo_path))
    _run_git(["git", "commit", "-m", "initial"], str(repo_path))
    _run_git(["git", "push", "origin", "main"], str(repo_path))
    return str(repo_path)


@pytest.fixture(scope="session")
def _migrated_database(tmp_path_factory) -> Path:
    """A database built the way production builds one — `alembic upgrade head`
    on an empty file — once per session. Each test gets a copy of it, so every
    test runs on the migrated schema and a model that drifts from the migration
    fails the whole suite, not one test."""
    from elstin.db.migrate import upgrade_to_head

    path = tmp_path_factory.mktemp("elstin-template") / "elstin.db"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    try:
        upgrade_to_head()
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    # Fold the write-ahead log into the one file that gets copied.
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture()
def database_url(_migrated_database: Path, tmp_path_factory: pytest.TempPathFactory) -> str:
    """This test's own copy of the migrated database (and so its own checkpoint
    file, which sits beside it). In a directory of its own, not the test's
    `tmp_path`: tests use that one as a repo or a data directory and list it."""
    path = tmp_path_factory.mktemp("elstin-db") / "elstin.db"
    shutil.copyfile(_migrated_database, path)
    return f"sqlite:///{path}"


async def fake_run_planner(**kwargs) -> dict:
    return {
        "questions": [],
        "context_bundle": {"fake": True, "project_path": kwargs.get("project_path")},
        "plan": {"summary": kwargs.get("user_request"), "files_to_create": [], "files_to_modify": []},
        "acceptance_criteria": kwargs.get("acceptance_criteria_hint") or [],
        "messages": [{"role": "planner", "content": "fake plan drafted"}],
    }


async def fake_run_dev(**kwargs) -> dict:
    return {
        "file_changes": [],
        "truncated": False,
        "messages": [{"role": "dev", "content": "fake dev: no changes made"}],
        "dev_history": [],
        "dev_build_runner": {},
    }


async def fake_run_deterministic_gate(**kwargs) -> dict:
    return {"passed": True, "issues": [], "required_fixes": [], "fake": True}


async def fake_run_verifier(**kwargs) -> dict:
    return {"verifier_report": {"passed": True, "fake": True}, "diffs": [], "messages": [{"role": "verifier", "content": "fake verifier: passed"}]}


async def fake_run_epic_qa(**kwargs) -> dict:
    return {
        "qa_report": {
            "fake": True,
            "verdict": "pass",
            "summary": "fake epic QA",
            "gaps": [],
            "full_test_suite": {"ran": True, "passed": True, "profiles": {}},
        },
        "messages": [{"role": "qa", "content": "fake qa: pass"}],
    }


@pytest.fixture()
def fake_agents(monkeypatch: pytest.MonkeyPatch):
    """Monkeypatches elstin.orchestration.graph's five entry points
    (run_planner/run_dev/run_deterministic_gate/run_verifier/run_epic_qa)
    with deterministic, no-network fakes — four are real LLM-provider
    callers in production (see elstin/agents/roles/*_loop.py, which route
    through elstin/agents/loop.py and elstin/llm/registry.py), and the
    Tier 1 gate runs the repo's real toolchain against a worktree these
    tests don't have, so tests must never invoke any of them for real.
    graph.py imported these names at module load time, so the patch
    target is the graph module's own attribute, not the origin module's."""
    import elstin.orchestration.graph as graph_module

    monkeypatch.setattr(graph_module, "run_planner", fake_run_planner)
    monkeypatch.setattr(graph_module, "run_dev", fake_run_dev)
    monkeypatch.setattr(graph_module, "run_deterministic_gate", fake_run_deterministic_gate)
    # The merge queue re-runs the gate on a story brought up to date with the
    # epic branch (elstin/daemon/main.py) — the same fake, imported there.
    import elstin.daemon.main as daemon_module

    monkeypatch.setattr(daemon_module, "run_deterministic_gate", fake_run_deterministic_gate)
    monkeypatch.setattr(graph_module, "run_verifier", fake_run_verifier)
    monkeypatch.setattr(graph_module, "run_epic_qa", fake_run_epic_qa)


@pytest.fixture()
def elstin_db(database_url: str, monkeypatch: pytest.MonkeyPatch):
    """Points elstin.db.engine at this test's own migrated database. Also
    resets the config cache so each test starts from a known-empty app_config
    table."""
    monkeypatch.setenv("DATABASE_URL", database_url)

    import elstin.db.engine as engine_mod

    engine_mod._engine = None
    engine_mod._SessionLocal = None

    import elstin.config.store as store_mod

    store_mod.invalidate_cache()

    yield

    store_mod.invalidate_cache()
    if engine_mod._engine is not None:
        engine_mod._engine.dispose()
    engine_mod._engine = None
    engine_mod._SessionLocal = None


@pytest.fixture()
def unreadable_database(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """A DATABASE_URL SQLite cannot open (the directory in its path is a file),
    for the tests of what code does when settings cannot be read."""
    import elstin.db.engine as engine_mod

    blocker = tmp_path_factory.mktemp("elstin-unreadable") / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{blocker}/elstin.db")
    engine_mod._engine = None
    engine_mod._SessionLocal = None
    yield
    engine_mod._engine = None
    engine_mod._SessionLocal = None


@pytest.fixture()
def project_id(elstin_db) -> int:
    """A bare project for tests that only need *a* project_id to satisfy
    Epic's now-required FK — tests exercising project behavior itself use
    StateManager().create_project(...) directly instead."""
    from elstin.state.manager import StateManager

    return StateManager().create_project("Test project")
