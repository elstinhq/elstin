"""What to do with a story whose run ended on a provider limit.

A model call refused because the account is out of quota, or rate-limited for
longer than is worth sitting through, used to leave the story plain "failed"
until a person noticed and pressed Retry. The provider often says when it will
accept calls again, and when it does there is nothing for a person to decide:
the story is parked, still "failed" so nothing downstream starts, with the
instant to retry at, and the daemon retries it itself (elstin/daemon/main.py's
`_resume_stories_past_their_limit`).

Only when the provider says when. A limit with no recoverable reset time gets no
automatic retry — guessing a window would turn one interruption into a loop
against a provider that is still refusing — and the story says so, so the person
who comes back knows whether to wait or to add credit.

A provider that could not be reached at all (kind "unreachable": the connection
was never made, after the loop had waited it out for a minute) is parked the same
way, with one difference: nothing says when it comes back, so the retry time is
elstin's own — a short fixed wait, tried a bounded number of times. Unlike a
limit that was never given a time, guessing is safe here: a failed connect costs
seconds, not a provider's goodwill, and no run is wasted. The alternative was the
story failing for a network blip and halting the epic until a person
noticed.

Pure: a limit and a few facts in, a decision and its wording out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

USAGE_LIMIT_REASON = "usage_limit"
# parked_reason of a story whose run ended because the model endpoint could not
# be reached. Swept with USAGE_LIMIT_REASON (elstin/state/manager_story.py).
UNREACHABLE_REASON = "provider_unreachable"

# An unreachable endpoint is retried on a fixed wait, not on a schedule the
# provider gave. Five minutes: long enough that a proxy restarting or a VPN
# reconnecting has finished, short enough that a story does not sit for an hour
# over a blip. Twelve tries is an hour of trying — past that it is not a blip,
# and a person should look rather than elstin keep knocking.
UNREACHABLE_RETRY_SECONDS = 300
MAX_AUTO_UNREACHABLE_RETRIES = 12

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
    # The story's parked_reason: what the board says it is waiting on, and which
    # sweep picks it up.
    reason: str = USAGE_LIMIT_REASON


def _utc(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _clock(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _plan_unreachable_parking(limit: dict[str, Any], *, retry_count: int, auto_resume: bool, now: datetime) -> Parking:
    said = (limit.get("message") or "").strip()
    quoted = f"The last error was: {said[:600]}" if said else ""
    what = "Model endpoint unreachable"
    if not auto_resume:
        return Parking(
            None, f"{what} — Retry when the connection is back",
            "This project does not run work on its own, so the story waits for a person. " + quoted,
            UNREACHABLE_REASON,
        )
    if retry_count >= MAX_AUTO_UNREACHABLE_RETRIES:
        return Parking(
            None, f"Model endpoint still unreachable — giving up on automatic retries after {retry_count}",
            "The connection has failed on every try for long enough that this is not a blip: check the endpoint, "
            "proxy or VPN, then Retry. " + quoted,
            UNREACHABLE_REASON,
        )
    retry_at = now + timedelta(seconds=UNREACHABLE_RETRY_SECONDS)
    return Parking(
        retry_at, f"{what} — retrying automatically at {_clock(retry_at)}",
        "The connection could not be made after several attempts. The story stays failed until then, so nothing that "
        "depends on it starts; the retry continues where the run stopped when it can, as pressing Retry would. " + quoted,
        UNREACHABLE_REASON,
    )


def plan_parking(limit: dict[str, Any], *, retry_count: int, auto_resume: bool, now: datetime | None = None) -> Parking:
    """`limit`: the run's `provider_limit` ({"kind", "reset_at", "message"}).
    `auto_resume`: whether the story's project lets the daemon progress work on
    its own (a manual-mode project's person is the one who presses Retry).
    `now` is only for an unreachable endpoint, whose retry time is elstin's own."""
    if limit.get("kind") == "unreachable":
        return _plan_unreachable_parking(limit, retry_count=retry_count, auto_resume=auto_resume, now=now or datetime.now(timezone.utc))
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
