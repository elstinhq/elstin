from __future__ import annotations

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import Epic, ProjectRepo

_TERMINAL_EPIC_PHASES = frozenset({"complete", "failed"})


class _EpicMixin:
    def list_active_epic_keys(self) -> list[str]:
        """Epics the daemon should still poll — everything not yet complete/failed."""
        with session_scope() as session:
            rows = session.execute(select(Epic.jira_key).where(Epic.phase.notin_(_TERMINAL_EPIC_PHASES))).scalars().all()
            return list(rows)

    def list_all_epic_keys(self) -> list[str]:
        """Every epic regardless of phase — for the dashboard, which shows
        completed/failed epics too, not just ones the daemon still polls."""
        with session_scope() as session:
            rows = session.execute(select(Epic.jira_key).order_by(Epic.created_at.desc())).scalars().all()
            return list(rows)

    def create_epic(
        self,
        jira_key: str,
        *,
        project_id: int,
        levels: list[list[str]] | None = None,
        summary: str = "",
        description: str | None = None,
        manual_gate: bool = True,
        external_source: str | None = None,
        external_id: str | None = None,
    ) -> None:
        """Insert a new epic row on first sync. Starts paused (`manual_gate=True`)
        so the daemon does not auto-run until a human resumes. Re-sync calls
        are idempotent — they must not reset phase, levels, or manual_gate."""
        with session_scope() as session:
            if session.get(Epic, jira_key) is not None:
                return
            session.add(Epic(
                jira_key=jira_key, project_id=project_id, levels=levels, phase="planning",
                summary=summary, description=description, manual_gate=manual_gate,
                external_source=external_source, external_id=external_id,
            ))

    def set_epic_project_repo(self, jira_key: str, project_repo_id: int | None) -> None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.project_repo_id = project_repo_id

    def get_epic(self, jira_key: str) -> dict | None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is None:
                return None
            return _epic_to_dict(session, epic)

    def update_epic_jira_fields(
        self, jira_key: str, *, summary: str, description: str | None
    ) -> None:
        """Refresh JIRA-sourced prose only. Phase, levels, gates, and
        folder assignment stay untouched — same contract as
        update_story_jira_fields."""
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.summary = summary
                epic.description = description

    def set_epic_levels(self, jira_key: str, levels: list[list[str]]) -> None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.levels = levels

    def set_epic_phase(self, jira_key: str, phase: str) -> None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.phase = phase

    def approve_epic_plan(self, jira_key: str) -> None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.plan_approved = True

    def set_epic_manual_gate(self, jira_key: str, manual_gate: bool) -> None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.manual_gate = manual_gate

    def set_integration_branch(self, jira_key: str, branch: str) -> None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.integration_branch = branch


def _epic_to_dict(session, epic: Epic) -> dict:
    """`"repo"` is computed, not stored — joined through project_repo_id to
    ProjectRepo.path (or None if no folder is chosen yet). This keeps every
    downstream consumer of epic["repo"] (forge/daemon/main.py's dispatch/
    merge/PR flow, forge/tools/worktree.py, forge/services/github_service.py)
    unchanged even though the folder is now a normalized reference."""
    repo_path = None
    if epic.project_repo_id is not None:
        project_repo = session.get(ProjectRepo, epic.project_repo_id)
        repo_path = project_repo.path if project_repo is not None else None

    return {
        "jira_key": epic.jira_key,
        "project_id": epic.project_id,
        "summary": epic.summary,
        "description": epic.description,
        "project_repo_id": epic.project_repo_id,
        "repo": repo_path,
        "phase": epic.phase,
        "levels": epic.levels or [],
        "integration_branch": epic.integration_branch,
        "plan_approved": epic.plan_approved,
        "manual_gate": epic.manual_gate,
        "external_source": epic.external_source,
        "external_id": epic.external_id,
        "external_synced_at": epic.external_synced_at.isoformat() if epic.external_synced_at else None,
    }
