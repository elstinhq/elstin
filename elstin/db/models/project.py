"""Projects — the grouping layer above Epic. A project holds one or more
local repo/folder checkouts (ProjectRepo), can override a small subset of
the global settings (ProjectConfig, mirroring AppConfig's shape — see
elstin/config/store.py's resolve_config for the override-with-fallback
semantics), and can carry one saved JIRA query (ProjectTicketFilter) that
pulls tickets in on its own instead of one key at a time."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from elstin.db.base import Base
from elstin.db.types import UtcDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which ticket source this project's epics and stories are pulled from
    # ("jira" today) — elstin/services/ticket_source.py resolves it to a
    # client. Existing projects are "jira".
    ticket_source: Mapped[str] = mapped_column(String(16), default="jira", server_default="jira", nullable=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    repos: Mapped[list["ProjectRepo"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectRepo(Base):
    """One folder of a project. Usually a git repo: cloned from `clone_url`
    into storage the app owns, or attached in place from a path already on
    disk (see elstin/services/repo_clone.py).

    `kind` "reference" is a plain folder read for context — docs, notes, a
    CLAUDE.md about the repos beside it — never written and never branched:
    runs read a filtered copy of it (elstin/tools/reference_folder.py).
    `read_only` marks a git repo the same way: readable by every run, never
    in a plan's files to change (elstin/agents/roles/plan_gate.py)."""

    __tablename__ = "project_repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    clone_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # "repo" | "reference" — see the class docstring.
    kind: Mapped[str] = mapped_column(String(16), default="repo", server_default="repo", nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # An AGENTS.md-style guide elstin wrote for a repo that had none of its
    # own (elstin/services/project_guide.py). Lives here rather than in the
    # repo so elstin never edits a checkout it does not own; a guide the repo
    # ships itself always wins over this one.
    generated_guide: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_guide_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="repos")


class ProjectConfig(Base):
    """A project-scoped override of one of elstin.config.store's
    PROJECT_OVERRIDABLE_KEYS. Absence of a row for (project_id, key) means
    "inherit the global AppConfig value" — this table only ever holds
    overrides, never a full copy of every setting."""

    __tablename__ = "project_config"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ProjectTicketFilter(Base):
    """The saved JIRA query a project ingests tickets from, instead of a
    human typing one issue key at a time into "Add ticket".

    One row per project (`project_id` is the PK, not an FK column on a
    surrogate id): a project has at most one filter, and "no row" means
    "this project only takes tickets added by hand". Making it a separate
    table rather than columns on `projects` keeps that absence cheap and
    keeps the sync bookkeeping (`last_*`) out of the row every projects
    listing reads.

    The structured fields are **not** JQL — they are compiled into JQL by
    elstin/services/jira_filter.py's build_jql, so the UI can offer plain
    fields ("project", "assignee") to people who don't write JQL, while
    `extra_jql` stays as the escape hatch for everything the fields don't
    cover (sprints, custom fields, date maths). Storing the parts rather
    than the compiled string is what lets the UI round-trip an edit.

    `enabled` gates only the *background* sweep (elstin/daemon/ticket_sync.py).
    A disabled filter is still saved, still previewable, and still runnable
    on demand via POST /api/projects/{id}/ticket-filter/sync — turning the
    background sweep off is not the same as throwing the query away.

    The `last_*` columns are observability, not state the sync depends on:
    every sweep re-derives what to do from JIRA plus the epics/stories
    already in the database, so a wiped or stale row here changes nothing about
    the outcome — it only changes what the UI can tell the user about the
    previous sweep (including `last_error`, which is otherwise a log line
    nobody sees, since the sweep runs unattended).
    """

    __tablename__ = "project_ticket_filters"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    jira_project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Whatever JIRA accepts on the right-hand side of `assignee = `: an
    # accountId, an email on servers that still allow it, or the literal
    # `currentUser()`. Not validated here — JIRA is the authority on what
    # resolves, and a wrong value surfaces as a preview error, not a silent
    # empty result.
    assignee: Mapped[str | None] = mapped_column(String(256), nullable=True)
    statuses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    labels: Mapped[list | None] = mapped_column(JSON, nullable=True)
    issue_types: Mapped[list | None] = mapped_column(JSON, nullable=True)
    extra_jql: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_matched_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_synced_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )
