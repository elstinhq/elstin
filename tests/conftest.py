from __future__ import annotations

import subprocess

import pytest
from testcontainers.postgres import PostgresContainer


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
def postgres_url() -> str:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


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
    """Monkeypatches forge.orchestration.graph's four agent entry points
    (run_planner/run_dev/run_verifier/run_epic_qa) with deterministic,
    no-network fakes — these are real LLM-provider callers in production
    (see forge/agents/roles/*_loop.py, which route through
    forge/agents/loop.py and forge/llm/registry.py), so tests must never
    invoke them for real. graph.py imported these names at module load
    time, so the patch target is the graph module's own attribute, not the
    origin module's."""
    import forge.orchestration.graph as graph_module

    monkeypatch.setattr(graph_module, "run_planner", fake_run_planner)
    monkeypatch.setattr(graph_module, "run_dev", fake_run_dev)
    monkeypatch.setattr(graph_module, "run_verifier", fake_run_verifier)
    monkeypatch.setattr(graph_module, "run_epic_qa", fake_run_epic_qa)


@pytest.fixture()
def forge_db(postgres_url: str, monkeypatch: pytest.MonkeyPatch):
    """Points forge.db.engine at the throwaway testcontainers Postgres,
    creates all tables, and tears down cleanly. Also resets the config
    cache so each test starts from a known-empty app_config table."""
    monkeypatch.setenv("DATABASE_URL", postgres_url)

    import forge.db.engine as engine_mod

    engine_mod._engine = None
    engine_mod._SessionLocal = None

    from forge.db.base import Base
    from forge.db.models import (  # noqa: F401
        ActivityEvent,
        AppConfig,
        Epic,
        JiraWatermark,
        Project,
        ProjectConfig,
        ProjectRepo,
        ProjectTicketFilter,
        PullRequest,
        Story,
        StoryUsage,
    )

    engine = engine_mod.get_engine()
    Base.metadata.create_all(engine)

    import forge.config.store as store_mod

    store_mod.invalidate_cache()

    yield

    Base.metadata.drop_all(engine)
    store_mod.invalidate_cache()
    engine_mod._engine = None
    engine_mod._SessionLocal = None


@pytest.fixture()
def project_id(forge_db) -> int:
    """A bare project for tests that only need *a* project_id to satisfy
    Epic's now-required FK — tests exercising project behavior itself use
    StateManager().create_project(...) directly instead."""
    from forge.state.manager import StateManager

    return StateManager().create_project("Test project")
