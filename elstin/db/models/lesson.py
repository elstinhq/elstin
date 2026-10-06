"""What elstin learned from its own runs, for a person to accept.

Every run starts cold: the project guide (elstin/services/project_guide.py)
is static, so a mistake elstin made on one story — the wrong JDK, a planned
file that already existed, build output staged in a pull request — is made
again on the next. The signals that say so are already recorded (gate
failures, verifier rejections, merge send-backs, a person's answers), and
nothing read them.

A lesson is one short, checkable line distilled from a finished story's
evidence by elstin/services/lessons.py. It is a *candidate* until a person
accepts it: the guide is read as authoritative, so a wrong line gets
followed, and nothing here reaches a prompt while `status` is "proposed".

Stored here, never in the repo — elstin does not edit checkouts it does not
own, the same rule as `ProjectRepo.generated_guide`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from elstin.db.base import Base

LESSON_PROPOSED = "proposed"
LESSON_ACCEPTED = "accepted"
LESSON_REJECTED = "rejected"
LESSON_STATUSES = (LESSON_PROPOSED, LESSON_ACCEPTED, LESSON_REJECTED)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectLesson(Base):
    __tablename__ = "project_lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # The repo the lesson is about; NULL when it holds for the whole project.
    # SET NULL rather than cascade: detaching a folder should not silently
    # delete what a person accepted, it just stops being scoped to it.
    project_repo_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_repos.id", ondelete="SET NULL"), nullable=True
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    # "environment" | "convention" | "gotcha" | "process" — a plain string,
    # like ActivityEvent.type: the extractor's vocabulary may grow.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="gotcha")
    # Path globs the lesson applies to (["src/**/*.vue"]); empty = everywhere.
    # Lets dev be handed only the lessons that touch the files it will write.
    scope_globs: Mapped[list | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=LESSON_PROPOSED, index=True)
    # Stories whose evidence produced (or later re-produced) this lesson.
    # `occurrences` is len-like but kept as a column so a list can be cut
    # without losing the count.
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    evidence_story_keys: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # The line of evidence the extractor quoted, so a reviewer can check the
    # lesson against what actually happened instead of trusting it.
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
