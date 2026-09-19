"""Pull requests, one per (story, repo).

A story whose approved plan writes two repos produces two branches and so
two pull requests — git's constraint, not a forge choice. Every accessor
here therefore takes a `repo_label` (forge/tools/workspace.py), with `""`
meaning "the story's default repo", which is what rows written before
per-repo PRs existed carry.

The label-free reads (`get_pull_request`, `list_pull_requests`) exist
because plenty of callers legitimately want "this story's PR" without
caring which repo — the dashboard, and the single-repo case that is still
the common one.
"""
from __future__ import annotations

from forge.db.engine import session_scope
from forge.db.models import PullRequest

# What a row carries when it predates per-repo pull requests, and what a
# caller that does not know or care about labels writes.
DEFAULT_REPO_LABEL = ""


def _to_dict(pr: PullRequest) -> dict:
    return {
        "story_key": pr.story_key,
        "repo_label": pr.repo_label,
        "number": pr.number,
        "url": pr.url,
        "state": pr.state,
    }


class _PullRequestMixin:
    def upsert_pull_request(
        self,
        story_key: str,
        *,
        repo_label: str = DEFAULT_REPO_LABEL,
        number: int | None = None,
        url: str | None = None,
        state: str = "open",
    ) -> None:
        with session_scope() as session:
            pr = session.query(PullRequest).filter_by(story_key=story_key, repo_label=repo_label).one_or_none()
            if pr is None:
                session.add(
                    PullRequest(story_key=story_key, repo_label=repo_label, number=number, url=url, state=state)
                )
            else:
                if number is not None:
                    pr.number = number
                if url is not None:
                    pr.url = url
                pr.state = state

    def get_pull_request(self, story_key: str, repo_label: str | None = None) -> dict | None:
        """One PR. With no label this returns the story's first PR, which
        is unambiguous for a single-repo story and is what the dashboard
        and the older call sites want."""
        with session_scope() as session:
            query = session.query(PullRequest).filter_by(story_key=story_key)
            if repo_label is not None:
                query = query.filter_by(repo_label=repo_label)
            pr = query.order_by(PullRequest.id).first()
            return _to_dict(pr) if pr is not None else None

    def list_pull_requests(self, story_key: str) -> list[dict]:
        """Every PR this story opened, oldest first. The daemon's CI gate
        and merge step both walk this, because a story that wrote two
        repos is only mergeable when *all* of its PRs are green."""
        with session_scope() as session:
            rows = session.query(PullRequest).filter_by(story_key=story_key).order_by(PullRequest.id).all()
            return [_to_dict(pr) for pr in rows]
