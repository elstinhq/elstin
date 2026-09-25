from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from forge.db.engine import session_scope
from forge.db.models import Epic, Project, ProjectRepo, ProjectTicketFilter


class _ProjectMixin:
    def create_project(self, name: str, *, description: str = "") -> int:
        with session_scope() as session:
            project = Project(name=name, description=description or None)
            session.add(project)
            session.flush()
            return project.id

    def list_projects(self) -> list[dict]:
        with session_scope() as session:
            projects = session.execute(select(Project).order_by(Project.created_at.desc())).scalars().all()
            repo_counts = dict(
                session.execute(select(ProjectRepo.project_id, func.count()).group_by(ProjectRepo.project_id)).all()
            )
            epic_counts = dict(session.execute(select(Epic.project_id, func.count()).group_by(Epic.project_id)).all())
            return [_project_to_dict(p, repo_counts.get(p.id, 0), epic_counts.get(p.id, 0)) for p in projects]

    def get_project(self, project_id: int) -> dict | None:
        with session_scope() as session:
            project = session.get(Project, project_id)
            if project is None:
                return None
            repo_count = session.scalar(select(func.count()).where(ProjectRepo.project_id == project_id)) or 0
            epic_count = session.scalar(select(func.count()).where(Epic.project_id == project_id)) or 0
            result = _project_to_dict(project, repo_count, epic_count)
            result["repos"] = [
                _project_repo_to_dict(r)
                for r in session.execute(select(ProjectRepo).where(ProjectRepo.project_id == project_id)).scalars()
            ]
            return result

    def get_project_ticket_source(self, project_id: int) -> str | None:
        with session_scope() as session:
            project = session.get(Project, project_id)
            return project.ticket_source if project is not None else None

    def update_project(
        self, project_id: int, *, name: str | None = None, description: str | None = None,
        ticket_source: str | None = None,
    ) -> None:
        with session_scope() as session:
            project = session.get(Project, project_id)
            if project is None:
                return
            if name is not None:
                project.name = name
            if description is not None:
                project.description = description or None
            if ticket_source is not None:
                project.ticket_source = ticket_source

    def add_project_repo(self, project_id: int, path: str, *, label: str | None = None, clone_url: str | None = None) -> int:
        with session_scope() as session:
            repo = ProjectRepo(project_id=project_id, path=path, label=label, clone_url=clone_url)
            session.add(repo)
            session.flush()
            return repo.id

    def list_project_repos(self, project_id: int) -> list[dict]:
        """Oldest first — this order is what picks the *default* repo for a
        new epic (the first-added one, see epic_sync.py) and what a
        multi-repo tool description lists as "the default", so it needs to
        be deterministic rather than whatever order Postgres feels like."""
        with session_scope() as session:
            rows = session.execute(
                select(ProjectRepo).where(ProjectRepo.project_id == project_id).order_by(ProjectRepo.id)
            ).scalars().all()
            return [_project_repo_to_dict(r) for r in rows]

    def get_project_repo(self, repo_id: int) -> dict | None:
        with session_scope() as session:
            repo = session.get(ProjectRepo, repo_id)
            return _project_repo_to_dict(repo) if repo is not None else None

    def set_project_repo_guide(self, repo_id: int, guide: str | None) -> bool:
        """Stores (or, with None/empty, discards) the guide forge generated
        for this repo. False if the repo does not exist."""
        with session_scope() as session:
            repo = session.get(ProjectRepo, repo_id)
            if repo is None:
                return False
            repo.generated_guide = guide or None
            repo.generated_guide_at = _utcnow() if guide else None
            return True

    def remove_project_repo(self, repo_id: int) -> bool:
        """Returns False (does not delete) if any epic still points at this
        folder — detaching it would silently orphan that epic's dispatch
        target."""
        with session_scope() as session:
            repo = session.get(ProjectRepo, repo_id)
            if repo is None:
                return False
            in_use = session.scalar(select(func.count()).where(Epic.project_repo_id == repo_id)) or 0
            if in_use:
                return False
            session.delete(repo)
            return True

    # -- ticket filters ---------------------------------------------------
    # One saved JIRA query per project (see forge/db/models/project.py's
    # ProjectTicketFilter). None means "no filter" — a distinct state from
    # "a filter that is switched off", which is a row with enabled=False.

    def get_project_ticket_filter(self, project_id: int) -> dict | None:
        with session_scope() as session:
            row = session.get(ProjectTicketFilter, project_id)
            return _ticket_filter_to_dict(row) if row is not None else None

    def set_project_ticket_filter(
        self,
        project_id: int,
        *,
        enabled: bool,
        jira_project_key: str | None = None,
        assignee: str | None = None,
        statuses: list[str] | None = None,
        labels: list[str] | None = None,
        issue_types: list[str] | None = None,
        extra_jql: str | None = None,
    ) -> dict:
        """Full replace, not a patch: the settings dialog always submits
        every field, and a partial update would make "clear the assignee"
        indistinguishable from "leave the assignee alone". The last_*
        bookkeeping is left untouched — it describes the last *run*, which
        an edit doesn't invalidate."""
        with session_scope() as session:
            row = session.get(ProjectTicketFilter, project_id)
            if row is None:
                row = ProjectTicketFilter(project_id=project_id)
                session.add(row)
            row.enabled = enabled
            row.jira_project_key = jira_project_key or None
            row.assignee = assignee or None
            row.statuses = list(statuses or []) or None
            row.labels = list(labels or []) or None
            row.issue_types = list(issue_types or []) or None
            row.extra_jql = extra_jql or None
            session.flush()
            return _ticket_filter_to_dict(row)

    def clear_project_ticket_filter(self, project_id: int) -> bool:
        with session_scope() as session:
            row = session.get(ProjectTicketFilter, project_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def list_enabled_ticket_filters(self) -> list[dict]:
        """What the background sweep iterates (forge/daemon/ticket_sync.py)."""
        with session_scope() as session:
            rows = session.execute(
                select(ProjectTicketFilter).where(ProjectTicketFilter.enabled.is_(True))
            ).scalars().all()
            return [_ticket_filter_to_dict(r) for r in rows]

    def record_ticket_filter_sync(
        self,
        project_id: int,
        *,
        matched: int | None = None,
        synced: int | None = None,
        error: str | None = None,
    ) -> None:
        """Observability only — see the model docstring. Always stamps
        last_synced_at, including on failure: "tried at 10:02 and failed"
        is the thing a user needs to see, and a filter that errors every
        sweep would otherwise look like it had never run at all."""
        with session_scope() as session:
            row = session.get(ProjectTicketFilter, project_id)
            if row is None:
                return
            row.last_synced_at = _utcnow()
            row.last_matched_count = matched
            row.last_synced_count = synced
            row.last_error = error


def _project_to_dict(project: Project, repo_count: int, epic_count: int) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "ticket_source": project.ticket_source,
        "repo_count": repo_count,
        "epic_count": epic_count,
        "created_at": project.created_at.isoformat(),
    }


def _project_repo_to_dict(repo: ProjectRepo) -> dict:
    return {
        "id": repo.id,
        "project_id": repo.project_id,
        "path": repo.path,
        "label": repo.label,
        "clone_url": repo.clone_url,
        "generated_guide": repo.generated_guide,
        "generated_guide_at": repo.generated_guide_at.isoformat() if repo.generated_guide_at else None,
        "created_at": repo.created_at.isoformat(),
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ticket_filter_to_dict(row: ProjectTicketFilter) -> dict:
    return {
        "project_id": row.project_id,
        "enabled": row.enabled,
        "jira_project_key": row.jira_project_key,
        "assignee": row.assignee,
        "statuses": list(row.statuses or []),
        "labels": list(row.labels or []),
        "issue_types": list(row.issue_types or []),
        "extra_jql": row.extra_jql,
        "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
        "last_matched_count": row.last_matched_count,
        "last_synced_count": row.last_synced_count,
        "last_error": row.last_error,
    }
