from __future__ import annotations

from forge.db.engine import session_scope
from forge.db.models import PullRequest


class _PullRequestMixin:
    def upsert_pull_request(self, story_key: str, *, number: int | None = None, url: str | None = None, state: str = "open") -> None:
        with session_scope() as session:
            pr = session.query(PullRequest).filter_by(story_key=story_key).one_or_none()
            if pr is None:
                session.add(PullRequest(story_key=story_key, number=number, url=url, state=state))
            else:
                if number is not None:
                    pr.number = number
                if url is not None:
                    pr.url = url
                pr.state = state

    def get_pull_request(self, story_key: str) -> dict | None:
        with session_scope() as session:
            pr = session.query(PullRequest).filter_by(story_key=story_key).one_or_none()
            if pr is None:
                return None
            return {"story_key": pr.story_key, "number": pr.number, "url": pr.url, "state": pr.state}
