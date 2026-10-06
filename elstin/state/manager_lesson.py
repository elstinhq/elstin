"""Project lessons — see elstin/db/models/lesson.py.

Kept apart from manager_project.py: those are a project's settings and
folders, these are what elstin proposes to add to its guide, with their own
review lifecycle."""
from __future__ import annotations

import re

from sqlalchemy import select

from elstin.db.engine import session_scope
from elstin.db.models import ProjectLesson
from elstin.db.models.lesson import LESSON_PROPOSED, LESSON_STATUSES

# A story that keeps producing the same lesson is evidence it is real, but
# the list of keys is for a reviewer to follow, not a log — cap it.
_MAX_EVIDENCE_KEYS = 10


def normalize_lesson_text(text: str) -> str:
    """The comparison key for "is this the lesson we already have": case,
    punctuation and spacing dropped, so a re-worded copy of the same line
    counts once instead of piling up as a new candidate."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def forget_evidence(session, story_keys: list[str] | set[str]) -> tuple[int, int]:
    """Detach removed stories from every lesson's evidence, inside the caller's
    transaction. Returns (lessons detached from, lessons dropped).

    A ticket that is removed takes its record with it — activity, usage,
    outcomes — so a lesson still citing it points at nothing a reviewer can
    open. Worse, a ticket added again under the same key would then look like
    the source of lessons learned on the deleted run, and an epic's pull
    request lists the lessons whose evidence includes one of its stories
    (elstin/services/lessons.py's pr_lessons_section).

    A lesson that is still only *proposed* and has no evidence left is dropped:
    nobody vouched for it and nothing supports it. One a person accepted or
    rejected is kept — that decision was theirs — with the removed stories
    taken out and its count reduced (never below 1: it was seen at least once)."""
    keys = set(story_keys)
    if not keys:
        return 0, 0
    detached = dropped = 0
    for row in session.execute(select(ProjectLesson)).scalars().all():
        evidence = list(row.evidence_story_keys or [])
        removed = [key for key in evidence if key in keys]
        if not removed:
            continue
        remaining = [key for key in evidence if key not in keys]
        if not remaining and row.status == LESSON_PROPOSED:
            session.delete(row)
            dropped += 1
            continue
        row.evidence_story_keys = remaining
        row.occurrences = max(1, (row.occurrences or 1) - len(removed))
        detached += 1
    return detached, dropped


class _LessonMixin:
    def add_lesson_candidate(
        self,
        project_id: int,
        text: str,
        *,
        project_repo_id: int | None = None,
        kind: str = "gotcha",
        scope_globs: list[str] | None = None,
        story_key: str | None = None,
        evidence_quote: str | None = None,
    ) -> dict:
        """Records a proposed lesson, or — when the project already has one
        with the same normalized text, whatever its status — counts this
        story as one more occurrence of it. A *rejected* lesson stays
        rejected: proposing it again must not put it back in front of a
        person who already said no."""
        key = normalize_lesson_text(text)
        with session_scope() as session:
            rows = session.execute(
                select(ProjectLesson).where(ProjectLesson.project_id == project_id)
            ).scalars().all()
            existing = next((r for r in rows if normalize_lesson_text(r.text) == key), None)
            if existing is not None:
                keys = list(existing.evidence_story_keys or [])
                if story_key and story_key not in keys:
                    keys.append(story_key)
                    existing.occurrences = (existing.occurrences or 1) + 1
                    existing.evidence_story_keys = keys[-_MAX_EVIDENCE_KEYS:]
                return _to_dict(existing) | {"created": False}
            row = ProjectLesson(
                project_id=project_id,
                project_repo_id=project_repo_id,
                text=text.strip(),
                kind=kind,
                scope_globs=list(scope_globs or []),
                status=LESSON_PROPOSED,
                occurrences=1,
                evidence_story_keys=[story_key] if story_key else [],
                evidence_quote=(evidence_quote or None),
            )
            session.add(row)
            session.flush()
            return _to_dict(row) | {"created": True}

    def list_project_lessons(self, project_id: int, *, status: str | None = None) -> list[dict]:
        """Oldest first, so the order a reviewer sees is stable."""
        with session_scope() as session:
            query = select(ProjectLesson).where(ProjectLesson.project_id == project_id)
            if status is not None:
                query = query.where(ProjectLesson.status == status)
            rows = session.execute(query.order_by(ProjectLesson.id)).scalars().all()
            return [_to_dict(r) for r in rows]

    def get_project_lesson(self, lesson_id: int) -> dict | None:
        with session_scope() as session:
            row = session.get(ProjectLesson, lesson_id)
            return _to_dict(row) if row is not None else None

    def set_lesson_status(self, lesson_id: int, status: str) -> bool:
        """False for an unknown lesson. An unknown status raises: it would
        otherwise be stored and never match anything that reads by status."""
        if status not in LESSON_STATUSES:
            raise ValueError(f"unknown lesson status {status!r}; expected one of {LESSON_STATUSES}")
        with session_scope() as session:
            row = session.get(ProjectLesson, lesson_id)
            if row is None:
                return False
            row.status = status
            return True

    def delete_lesson(self, lesson_id: int) -> bool:
        """Forgets a lesson outright. Unlike rejecting, this lets the same
        line be proposed again later. False for an unknown lesson."""
        with session_scope() as session:
            row = session.get(ProjectLesson, lesson_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def update_lesson_text(self, lesson_id: int, text: str) -> bool:
        """A person's edit before accepting. False for an unknown lesson."""
        text = text.strip()
        if not text:
            raise ValueError("a lesson cannot be empty")
        with session_scope() as session:
            row = session.get(ProjectLesson, lesson_id)
            if row is None:
                return False
            row.text = text
            return True


def _to_dict(row: ProjectLesson) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "project_repo_id": row.project_repo_id,
        "text": row.text,
        "kind": row.kind,
        "scope_globs": list(row.scope_globs or []),
        "status": row.status,
        "occurrences": row.occurrences,
        "evidence_story_keys": list(row.evidence_story_keys or []),
        "evidence_quote": row.evidence_quote,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
