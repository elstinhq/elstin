"""PO sessions and their conversation — see elstin/db/models/po_session.py.

Every transition is guarded by the status the row is in *inside the same
transaction that changes it* (row-locked), because three things move a session
at once: the API (a person's edit, message, answer, publish or cancel), the
daemon's runner (a turn starting and finishing) and its publisher. A guard that
read the status first and wrote after would let a person's message land in the
gap between "the turn is finishing" and "the turn is finished" and be lost.

Kept apart from manager_lesson.py / manager_project.py: a session has its own
lifecycle (queued → running → ready → published) and its own telemetry to
clean up when it goes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select

from elstin.db.engine import session_scope
from elstin.db.models import ActivityEvent, PoMessage, PoSession, StoryRunOutcome, StoryUsage
# Re-exported on purpose: the rest of elstin reaches a session's vocabulary (its
# statuses, its modes, its run id) through here, because only state/ may import
# elstin.db.models (AGENTS.md hard constraint #1).
from elstin.db.models.po_session import (  # noqa: F401
    PO_AWAITING_INPUT, PO_DISCARDED, PO_FAILED, PO_MODE_BRAINSTORM, PO_MODE_BUG, PO_MODE_GAP, PO_MODE_SPLIT, PO_MODES, PO_PUBLISHED,
    PO_PUBLISHING, PO_QUEUED, PO_READY, PO_RUNNING, PO_STATUSES, ROLE_AGENT, ROLE_PERSON, po_run_id,
)


class PoSessionConflict(RuntimeError):
    """The session is not in a state that allows this, or the draft a save was
    based on is no longer the current one. The message says which."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def every_ticket_created(draft: Any, mode: str, created: dict[str, str]) -> bool:
    """Whether everything the draft proposes is in JIRA by now: every story, and
    the epic of a brainstorm — or a bug draft's one ticket. `created` is the
    checkpoint's {ref -> key}."""
    if not isinstance(draft, dict):
        return False
    if mode == PO_MODE_BUG:
        return "bug" in created
    if mode == PO_MODE_BRAINSTORM and "epic" not in created:
        return False
    refs = [s.get("ref") for s in draft.get("stories") or [] if isinstance(s, dict)]
    return bool(refs) and all(ref in created for ref in refs)


def _session_to_dict(row: PoSession) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "epic_key": row.epic_key,
        "jira_project_key": row.jira_project_key,
        "mode": row.mode,
        "brief": row.brief,
        "status": row.status,
        "context": row.context,
        "draft": row.draft,
        "draft_version": row.draft_version,
        "edited_by_person": bool(row.edited_by_person),
        "person_edits": list(row.person_edits or []),
        "questions": list(row.questions or []),
        "answers": list(row.answers or []),
        "publish_result": row.publish_result,
        "error": row.error,
        "cost_usd": float(row.cost_usd or 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _message_to_dict(row: PoMessage) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "role": row.role,
        "text": row.text,
        "draft_version": row.draft_version,
        "delivered": row.delivered_at is not None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def delete_po_sessions_of_project(session, project_id: int) -> int:
    """Delete every PO session of `project_id` — rows, messages (cascade) and
    their telemetry — inside the caller's transaction. Returns how many."""
    ids = list(session.execute(select(PoSession.id).where(PoSession.project_id == project_id)).scalars())
    _delete_sessions(session, ids)
    return len(ids)


def _delete_sessions(session, ids: list[int]) -> dict[str, int]:
    if not ids:
        return {"sessions": 0, "messages": 0, "activity_events": 0, "usage_rows": 0, "run_outcomes": 0}
    run_ids = [po_run_id(i) for i in ids]

    def gone(statement) -> int:
        return session.execute(statement).rowcount or 0

    return {
        "activity_events": gone(delete(ActivityEvent).where(ActivityEvent.run_id.in_(run_ids))),
        "usage_rows": gone(delete(StoryUsage).where(StoryUsage.run_id.in_(run_ids))),
        "run_outcomes": gone(delete(StoryRunOutcome).where(StoryRunOutcome.run_id.in_(run_ids))),
        "messages": gone(delete(PoMessage).where(PoMessage.session_id.in_(ids))),
        "sessions": gone(delete(PoSession).where(PoSession.id.in_(ids))),
    }


class _PoMixin:
    # -- create / read ---------------------------------------------------------

    def create_po_session(
        self, project_id: int, *, brief: str, mode: str, epic_key: str | None = None,
        jira_project_key: str | None = None, context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A queued session, with the person's brief as the first line of its
        conversation (already read: it is in the session, and the first turn
        is given it that way)."""
        with session_scope() as session:
            row = PoSession(
                project_id=project_id, epic_key=epic_key, jira_project_key=jira_project_key, mode=mode, brief=brief,
                context=context, status=PO_QUEUED, draft_version=0, edited_by_person=False, cost_usd=0,
            )
            session.add(row)
            session.flush()
            session.add(PoMessage(session_id=row.id, role=ROLE_PERSON, text=brief, delivered_at=_now()))
            session.flush()
            return _session_to_dict(row)

    def get_po_session(self, session_id: int, *, messages: bool = False) -> dict[str, Any] | None:
        with session_scope() as session:
            row = session.get(PoSession, session_id)
            if row is None:
                return None
            data = _session_to_dict(row)
            if messages:
                data["messages"] = self._po_messages(session, session_id)
            return data

    @staticmethod
    def _po_messages(session, session_id: int) -> list[dict[str, Any]]:
        rows = session.execute(
            select(PoMessage).where(PoMessage.session_id == session_id).order_by(PoMessage.id)
        ).scalars()
        return [_message_to_dict(row) for row in rows]

    def list_po_messages(self, session_id: int) -> list[dict[str, Any]]:
        with session_scope() as session:
            return self._po_messages(session, session_id)

    def list_po_sessions(self, project_id: int, *, include_discarded: bool = False) -> list[dict[str, Any]]:
        with session_scope() as session:
            query = select(PoSession).where(PoSession.project_id == project_id)
            if not include_discarded:
                query = query.where(PoSession.status != PO_DISCARDED)
            rows = session.execute(query.order_by(PoSession.id.desc())).scalars().all()
            return [_session_to_dict(row) for row in rows]

    def count_running_po_sessions(self, project_id: int) -> int:
        """Sessions of `project_id` with work in flight — a turn or a publish."""
        with session_scope() as session:
            return session.execute(
                select(func.count()).select_from(PoSession).where(
                    PoSession.project_id == project_id, PoSession.status.in_((PO_RUNNING, PO_PUBLISHING)),
                )
            ).scalar_one()

    # -- the runner's side -------------------------------------------------------

    def claim_next_po_session(self) -> dict[str, Any] | None:
        """The oldest queued session, now `running`, or None. Row-locked and
        skipping a locked row, so two claimers can never take the same one."""
        with session_scope() as session:
            row = session.execute(
                select(PoSession).where(PoSession.status == PO_QUEUED).order_by(PoSession.id)
                .limit(1).with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = PO_RUNNING
            row.error = None
            session.flush()
            return _session_to_dict(row)

    def requeue_orphaned_po_sessions(self, live_ids: set[int] | frozenset[int]) -> list[int]:
        """Sessions left `running` by a daemon that died: no task in this
        process is working on them. They are queued again, and the person
        messages their lost turn had read are made unread, so the new turn
        reads them. The opening brief is not one of those — it travels in the
        session itself."""
        requeued: list[int] = []
        with session_scope() as session:
            rows = session.execute(
                select(PoSession).where(PoSession.status == PO_RUNNING).with_for_update()
            ).scalars().all()
            for row in rows:
                if row.id in live_ids:
                    continue
                row.status = PO_QUEUED
                first_id = session.execute(
                    select(func.min(PoMessage.id)).where(PoMessage.session_id == row.id)
                ).scalar()
                last_agent_id = session.execute(
                    select(func.max(PoMessage.id)).where(PoMessage.session_id == row.id, PoMessage.role == ROLE_AGENT)
                ).scalar()
                floor = max(first_id or 0, last_agent_id or 0)
                for message in session.execute(
                    select(PoMessage).where(
                        PoMessage.session_id == row.id, PoMessage.role == ROLE_PERSON, PoMessage.id > floor,
                    )
                ).scalars():
                    message.delivered_at = None
                requeued.append(row.id)
        return requeued

    def take_po_messages(self, session_id: int) -> list[dict[str, Any]]:
        """Every message of a person's the agent has not read yet, now read.
        Row-locked, so a message is delivered once and never lost."""
        with session_scope() as session:
            rows = session.execute(
                select(PoMessage).where(
                    PoMessage.session_id == session_id, PoMessage.role == ROLE_PERSON, PoMessage.delivered_at.is_(None),
                ).order_by(PoMessage.id).with_for_update()
            ).scalars().all()
            for row in rows:
                row.delivered_at = _now()
            return [_message_to_dict(row) for row in rows]

    def has_unread_po_messages(self, session_id: int) -> bool:
        with session_scope() as session:
            return session.execute(
                select(func.count()).select_from(PoMessage).where(
                    PoMessage.session_id == session_id, PoMessage.role == ROLE_PERSON, PoMessage.delivered_at.is_(None),
                )
            ).scalar_one() > 0

    def set_po_context(self, session_id: int, context: dict[str, Any]) -> None:
        with session_scope() as session:
            row = session.get(PoSession, session_id)
            if row is not None:
                row.context = context

    def finish_po_turn(
        self, session_id: int, *, draft: dict[str, Any], questions: list[dict[str, Any]], reply: str, cost_usd: float,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """A turn's result: the draft (a new version), its questions, the
        agent's reply in the conversation. Where the session goes next:
        `awaiting_input` when the agent asked something, else `ready` — or
        `queued` again when a person wrote while the turn ran and the turn
        ended before reading it. None when the session is no longer running
        (cancelled or deleted mid-turn): the result is dropped."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None or row.status != PO_RUNNING:
                return None
            row.draft = draft
            if context is not None:
                row.context = context
            row.draft_version = (row.draft_version or 0) + 1
            row.questions = questions or []
            row.answers = None
            row.error = None
            row.cost_usd = cost_usd
            session.add(PoMessage(session_id=session_id, role=ROLE_AGENT, text=reply or "", draft_version=row.draft_version))
            unread = session.execute(
                select(func.count()).select_from(PoMessage).where(
                    PoMessage.session_id == session_id, PoMessage.role == ROLE_PERSON, PoMessage.delivered_at.is_(None),
                )
            ).scalar_one()
            row.status = PO_QUEUED if unread else (PO_AWAITING_INPUT if questions else PO_READY)
            session.flush()
            return _session_to_dict(row)

    def fail_po_session(self, session_id: int, error: str, *, cost_usd: float | None = None) -> dict[str, Any] | None:
        """A turn that produced nothing usable. The draft it started from, if
        any, is kept: what a person could already see does not vanish."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None or row.status != PO_RUNNING:
                return None
            row.status = PO_FAILED
            row.error = error[:4000]
            if cost_usd is not None:
                row.cost_usd = cost_usd
            session.flush()
            return _session_to_dict(row)

    def set_po_cost(self, session_id: int, cost_usd: float) -> None:
        with session_scope() as session:
            row = session.get(PoSession, session_id)
            if row is not None:
                row.cost_usd = cost_usd

    # -- the API's side ------------------------------------------------------------

    def save_po_draft(
        self, session_id: int, draft: dict[str, Any], *, expected_version: int, edited_paths: list[str],
    ) -> dict[str, Any]:
        """A person's edit. Refused (PoSessionConflict) while the agent is
        working or the session is finished, and when `expected_version` is not
        the draft's current one — the edit was made over a draft that has since
        changed, and saving it would silently throw the newer one away."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                raise PoSessionConflict("No such session.")
            if row.status in (PO_RUNNING, PO_PUBLISHING, PO_PUBLISHED, PO_DISCARDED):
                raise PoSessionConflict(f"The draft cannot be edited while the session is {row.status}.")
            if row.draft is None:
                raise PoSessionConflict("There is no draft to edit yet.")
            if (row.draft_version or 0) != expected_version:
                raise PoSessionConflict(
                    f"The draft has changed since you opened it (now version {row.draft_version}, yours was "
                    f"{expected_version}). Reload it and make the edit again."
                )
            row.draft = draft
            row.draft_version = (row.draft_version or 0) + 1
            row.edited_by_person = True
            seen = list(row.person_edits or [])
            row.person_edits = seen + [path for path in dict.fromkeys(edited_paths) if path not in seen]
            session.flush()
            return _session_to_dict(row)

    def add_po_message(self, session_id: int, text: str) -> dict[str, Any] | None:
        """A person's message, unread. None for an unknown session."""
        with session_scope() as session:
            if session.get(PoSession, session_id) is None:
                return None
            row = PoMessage(session_id=session_id, role=ROLE_PERSON, text=text)
            session.add(row)
            session.flush()
            return _message_to_dict(row)

    def queue_po_turn(self, session_id: int, *, from_statuses: tuple[str, ...]) -> dict[str, Any] | None:
        """Queue another turn if the session is in one of `from_statuses`; the
        session as it is now either way, None for an unknown one. The caller
        looks at `status` to see whether it took."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                return None
            if row.status in from_statuses:
                row.status = PO_QUEUED
                row.error = None
                session.flush()
            return _session_to_dict(row)

    def set_po_answers(self, session_id: int, answers: list[dict[str, Any]]) -> dict[str, Any]:
        """The person's answers to the agent's questions, and another turn to
        use them. Only while the agent is waiting on them (or has a finished
        draft it asked something about)."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                raise PoSessionConflict("No such session.")
            if row.status not in (PO_AWAITING_INPUT, PO_READY):
                raise PoSessionConflict(f"The agent is not waiting for answers (the session is {row.status}).")
            if not row.questions:
                raise PoSessionConflict("The agent has no open questions.")
            row.answers = answers
            row.status = PO_QUEUED
            row.error = None
            session.flush()
            return _session_to_dict(row)

    def discard_po_session(self, session_id: int) -> dict[str, Any]:
        """The session is dropped from the open list. Not while a publish is in
        flight (it would leave tickets half made with nothing recording which),
        and not once published — that is a record of what was created."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                raise PoSessionConflict("No such session.")
            if row.status in (PO_PUBLISHING, PO_PUBLISHED):
                raise PoSessionConflict(f"A session that is {row.status} cannot be discarded.")
            row.status = PO_DISCARDED
            session.flush()
            return _session_to_dict(row)

    def delete_po_session(self, session_id: int) -> dict[str, int] | None:
        """Remove the session, its conversation and its telemetry. None for an
        unknown one. A publish in flight refuses it, for the reason above."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                return None
            if row.status == PO_PUBLISHING:
                raise PoSessionConflict("A session that is publishing cannot be deleted.")
            return _delete_sessions(session, [session_id])

    # -- publishing -----------------------------------------------------------------------
    # `publish_result` is the publisher's checkpoint: {created: {ref -> JIRA key},
    # links: ["blocker>blocked", ...], comments: [key, ...], epic_key, synced,
    # failed}. Every ticket is recorded the moment it exists, so a publish that
    # stops half-way continues where it was and never makes a duplicate.

    def begin_po_publish(self, session_id: int, *, jira_project_key: str | None = None) -> dict[str, Any]:
        """`ready` → `publishing`, once. The draft is frozen from here (a save is
        refused while the status is `publishing`)."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                raise PoSessionConflict("No such session.")
            if row.status != PO_READY:
                raise PoSessionConflict(f"Only a draft that is ready can be published (the session is {row.status}).")
            if jira_project_key:
                row.jira_project_key = jira_project_key
            row.status = PO_PUBLISHING
            row.error = None
            row.publish_result = {**(row.publish_result or {}), "started": True, "failed": None}
            session.flush()
            return _session_to_dict(row)

    def record_po_publish(
        self, session_id: int, *, created: dict[str, str] | None = None, link: str | None = None,
        comment: str | None = None, **fields: Any,
    ) -> dict[str, Any] | None:
        """Add to the checkpoint. Row-locked and merged, never replaced, so the
        publisher and a person posting a suggestion cannot overwrite each other."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                return None
            result = dict(row.publish_result or {})
            if created:
                result["created"] = {**(result.get("created") or {}), **created}
            if link and link not in (result.get("links") or []):
                result["links"] = [*(result.get("links") or []), link]
            if comment and comment not in (result.get("comments") or []):
                result["comments"] = [*(result.get("comments") or []), comment]
            result.update(fields)
            row.publish_result = result
            session.flush()
            return _session_to_dict(row)

    def finish_po_publish(self, session_id: int, *, scoped: bool = False) -> dict[str, Any] | None:
        """A publish that ran to its end. `published` — the draft is then a record
        and frozen — unless it was `scoped` to some stories and others are still
        not in JIRA: then the session is `ready` again, for the next story."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None or row.status != PO_PUBLISHING:
                return None
            created = (row.publish_result or {}).get("created") or {}
            row.status = PO_PUBLISHED if not scoped or every_ticket_created(row.draft, row.mode, created) else PO_READY
            row.error = None
            session.flush()
            return _session_to_dict(row)

    def fail_po_publish(self, session_id: int, error: str) -> dict[str, Any] | None:
        """A publish that stopped. Back to `ready` — what was created is in the
        checkpoint, so pressing Publish again continues rather than repeats."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None or row.status != PO_PUBLISHING:
                return None
            row.status = PO_READY
            row.error = error[:4000]
            session.flush()
            return _session_to_dict(row)

    def recover_interrupted_publishes(self, live_ids: set[int] | frozenset[int]) -> list[int]:
        """Sessions left `publishing` by a daemon that died. They go back to
        `ready` with the reason — not resumed on their own: a person pressed
        Publish once, and pressing it again is how it continues."""
        recovered: list[int] = []
        with session_scope() as session:
            rows = session.execute(select(PoSession).where(PoSession.status == PO_PUBLISHING).with_for_update()).scalars().all()
            for row in rows:
                if row.id in live_ids:
                    continue
                row.status = PO_READY
                row.error = (
                    "Publishing was interrupted (elstin restarted). What was already created is kept — press Publish to "
                    "continue; nothing is made twice."
                )
                recovered.append(row.id)
        return recovered

    def dismiss_po_questions(self, session_id: int) -> dict[str, Any]:
        """Let the draft stand on the agent's own best guesses: its open questions
        are dropped and the session is `ready`. Without this a draft the agent
        asked something about could never be published unless it were answered."""
        with session_scope() as session:
            row = session.execute(select(PoSession).where(PoSession.id == session_id).with_for_update()).scalar_one_or_none()
            if row is None:
                raise PoSessionConflict("No such session.")
            if row.status != PO_AWAITING_INPUT:
                raise PoSessionConflict(f"The agent is not waiting for answers (the session is {row.status}).")
            row.questions = []
            row.status = PO_READY
            session.flush()
            return _session_to_dict(row)

    def set_po_jira_project_key(self, session_id: int, jira_project_key: str) -> None:
        with session_scope() as session:
            row = session.get(PoSession, session_id)
            if row is not None:
                row.jira_project_key = jira_project_key
