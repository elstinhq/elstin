"""What to do with a story whose run ended on a provider limit.

A model call refused because the account is out of quota, or rate-limited for
longer than is worth sitting through, used to leave the story plain "failed"
until a person noticed and pressed Retry. The provider often says when it will
accept calls again, and when it does there is nothing for a person to decide:
the story is parked, still "failed" so nothing downstream starts, with the
instant to retry at, and the daemon retries it itself (forge/daemon/main.py's
`_resume_stories_past_their_limit`).

Only when the provider says when. A limit with no recoverable reset time gets no
automatic retry — guessing a window would turn one interruption into a loop
against a provider that is still refusing — and the story says so, so the person
who comes back knows whether to wait or to add credit.

Pure: a limit and a few facts in, a decision and its wording out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

USAGE_LIMIT_REASON = "usage_limit"

# Automatic retries for one story, counted on `stories.retry_count` (a person's
# Retry counts too). A daily limit legitimately parks a story once a day; a story
# that keeps hitting a limit it was supposed to have waited out is not going to
# get further by being retried again.
MAX_AUTO_LIMIT_RETRIES = 20


@dataclass(frozen=True)
class Parking:
    retry_at: datetime | None
    title: str
    detail: str


def _utc(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _clock(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def plan_parking(limit: dict[str, Any], *, retry_count: int, auto_resume: bool) -> Parking:
    """`limit`: the run's `provider_limit` ({"kind", "reset_at", "message"}).
    `auto_resume`: whether the story's project lets the daemon progress work on
    its own (a manual-mode project's person is the one who presses Retry)."""
    reset_at = limit.get("reset_at")
    quota = limit.get("kind") == "quota"
    what = "Provider is out of credit or quota" if quota else "Provider rate limit reached"
    said = (limit.get("message") or "").strip()
    quoted = f"The provider said: {said[:600]}" if said else ""

    if reset_at is None:
        advice = "add credit, then Retry" if quota else "Retry later"
        return Parking(
            None, f"{what} — it did not say when that lifts; {advice}",
            "No automatic retry: guessing a window would retry into a provider that is still refusing. " + quoted,
        )

    lifts = _utc(float(reset_at))
    if not auto_resume:
        return Parking(
            None, f"{what} — it lifts at {_clock(lifts)}; Retry then",
            "This project does not run work on its own, so the story waits for a person. " + quoted,
        )
    if retry_count >= MAX_AUTO_LIMIT_RETRIES:
        return Parking(
            None, f"{what} again — giving up on automatic retries after {retry_count}",
            "It lifts at " + _clock(lifts) + ", but this story has already been retried too often to keep doing it unattended. " + quoted,
        )
    return Parking(
        lifts, f"{what} — retrying automatically at {_clock(lifts)}",
        "The story stays failed until then, so nothing that depends on it starts. " + quoted,
    )
