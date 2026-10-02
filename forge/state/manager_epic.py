from __future__ import annotations

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import Epic, EpicPullRequest, ProjectRepo

# Epics the daemon no longer polls. "pr_open" is NOT terminal: the epic's
# pull request is still refreshed (CI status, merged or closed) every poll.
_TERMINAL_EPIC_PHASES = frozenset({"complete", "merged", "failed"})


class _EpicMixin:
    def list_active_epic_keys(self) -> list[str]:
        """Epics the daemon should still poll — everything not yet complete/failed."""
        with session_scope() as session:
            rows = session.execute(select(Epic.jira_key).where(Epic.phase.notin_(_TERMINAL_EPIC_PHASES))).scalars().all()
            return list(rows)

    def list_project_epic_keys(self, project_id: int) -> list[str]:
        """Every epic of one project, whatever its phase."""
        with session_scope() as session:
            rows = session.execute(
                select(Epic.jira_key).where(Epic.project_id == project_id).order_by(Epic.created_at)
            ).scalars().all()
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
        project_repo_id: int | None = None,
    ) -> None:
        """Insert a new epic row on first sync. Starts paused (`manual_gate=True`)
        so the daemon does not auto-run until a human resumes. Re-sync calls
        are idempotent — they must not reset phase, levels, or manual_gate.

        `project_repo_id` is the caller's choice of default repo (typically
        the project's first-added one — see epic_sync.py); it is only a
        default for unprefixed paths (forge/tools/workspace.py), never a
        binding, but dispatch still needs *some* real repo path to create
        the story's first worktree from, so a project with repos always
        gets one assigned rather than waiting on a human to pick."""
        with session_scope() as session:
            if session.get(Epic, jira_key) is not None:
                return
            session.add(Epic(
                jira_key=jira_key, project_id=project_id, levels=levels, phase="planning",
                summary=summary, description=description, manual_gate=manual_gate,
                external_source=external_source, external_id=external_id,
                project_repo_id=project_repo_id,
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

    def list_epic_pull_requests(self, epic_key: str) -> list[dict]:
        with session_scope() as session:
            rows = session.execute(
                select(EpicPullRequest).where(EpicPullRequest.epic_key == epic_key).order_by(EpicPullRequest.repo_label)
            ).scalars().all()
            return [_epic_pull_request_to_dict(row) for row in rows]

    def upsert_epic_pull_request(self, epic_key: str, repo_label: str, **fields) -> None:
        """Create or update the epic's pull request row for one repo. Only the
        fields passed are written — `error=None` clears a recorded error."""
        allowed = {"repo_path", "branch", "number", "url", "state", "ci_status", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown epic pull request field(s): {sorted(unknown)}")
        with session_scope() as session:
            row = session.execute(
                select(EpicPullRequest).where(
                    EpicPullRequest.epic_key == epic_key, EpicPullRequest.repo_label == repo_label,
                )
            ).scalar_one_or_none()
            if row is None:
                row = EpicPullRequest(epic_key=epic_key, repo_label=repo_label)
                session.add(row)
            for name, value in fields.items():
                setattr(row, name, value)

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

    def set_epic_pr_approval(self, jira_key: str, value: str | None) -> None:
        """"awaiting" | "approved" | "skipped" | None — see Epic.pr_approval."""
        if value not in (None, "awaiting", "approved", "skipped"):
            raise ValueError(f"unknown PR approval state: {value!r}")
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.pr_approval = value

    def set_epic_prior_work(self, jira_key: str, prior_work: dict | None) -> None:
        """The lookup for the epic's own key (forge/services/prior_work.py),
        or None to look again."""
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.prior_work = dict(prior_work) if prior_work is not None else None

    def set_integration_branch(self, jira_key: str, branch: str) -> None:
        with session_scope() as session:
            epic = session.get(Epic, jira_key)
            if epic is not None:
                epic.integration_branch = branch


def _epic_pull_request_to_dict(row: EpicPullRequest) -> dict:
    return {
        "repo_label": row.repo_label,
        "repo_path": row.repo_path,
        "branch": row.branch,
        "number": row.number,
        "url": row.url,
        "state": row.state,
        "ci_status": row.ci_status,
        "error": row.error,
    }


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
        "prior_work": epic.prior_work,
        "plan_approved": epic.plan_approved,
        "manual_gate": epic.manual_gate,
        "pr_approval": epic.pr_approval,
        "external_source": epic.external_source,
        "external_id": epic.external_id,
        "external_synced_at": epic.external_synced_at.isoformat() if epic.external_synced_at else None,
        "external_hash": epic.external_hash,
        "external_status": epic.external_status,
        "external_closed": epic.external_closed,
    }
