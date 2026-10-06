"""Provider-agnostic exception hierarchy. elstin/agents/loop.py catches
ONLY these — never a provider SDK's own exception classes — so adding a
second adapter never means touching the loop's retry logic.

Each adapter (elstin/llm/openai_compat.py today) is responsible for
catching its own SDK's typed exceptions and re-raising the matching class
here — so "is this retryable?" is answered by the provider's own error
type, never by substring-matching an error message, which is what the
pre-migration code had to do when its only signal was a result string.

The one exception is LlmContextOverflowError: no provider SDK has a typed
error for "the prompt is too long" — it arrives as an ordinary 400 — so an
adapter recognizes it by error code where there is one and by
`is_context_overflow_message` otherwise.
"""
from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


class LlmError(RuntimeError):
    """Base class for every error a provider adapter can raise."""


class LlmAuthError(LlmError):
    """Missing or invalid credentials. Never retryable."""


class LlmBadRequestError(LlmError):
    """The request itself was rejected (bad schema, invalid params, ...).
    Never retryable without changing the request."""


class LlmContextOverflowError(LlmBadRequestError):
    """The prompt no longer fits the model's context window. Not retryable
    as-is — but elstin/agents/loop.py can shrink the history and send the
    same turn again once, which is why it is its own class."""


# How providers word "the prompt is too long", for adapters whose SDK has
# no typed error for it (an OpenAI-compatible gateway answers with a plain
# 400 whose message is the only signal). Taken from opencode's
# provider-error.ts (github.com/anomalyco/opencode, MIT), which collects
# them across OpenAI, Anthropic, Gemini, Bedrock, vLLM, Ollama and others.
_CONTEXT_OVERFLOW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"prompt is too long",
        r"input is too long for requested model",
        r"exceeds the context window",
        r"exceeds (?:the )?(?:model'?s )?maximum context length",
        r"input token count.*exceeds the maximum",
        r"tokens in request more than max tokens allowed",
        r"maximum prompt length is \d+",
        r"reduce the length of the messages",
        r"maximum context length is \d+ tokens",
        r"exceeds (?:the )?maximum allowed input length",
        r"is longer than the model'?s context length",
        r"exceeds the available context size",
        r"greater than the context length",
        r"context window exceeds limit",
        r"exceeded model token limit",
        r"context[_ ]length[_ ]exceeded",
        r"context length is only \d+ tokens",
        r"input length.*exceeds.*context length",
        r"prompt too long; exceeded (?:max )?context length",
        r"too large for model with \d+ maximum context length",
        r"but the configured context size is",
        r"model_context_window_exceeded",
    )
)
# A rate limit can say "too many tokens" too; that one is retryable as-is.
_NOT_OVERFLOW_PATTERNS = (re.compile(r"rate limit", re.IGNORECASE), re.compile(r"too many requests", re.IGNORECASE))


def is_context_overflow_message(message: str) -> bool:
    return not any(p.search(message) for p in _NOT_OVERFLOW_PATTERNS) and any(
        p.search(message) for p in _CONTEXT_OVERFLOW_PATTERNS
    )


class LlmLimitError(LlmError):
    """The provider is refusing calls for now because of a limit on the account
    or key, not because of anything wrong with the request. `retry_after_seconds`
    is when the provider says it will accept calls again — from a Retry-After
    header, or the reset time its message names — or None when it does not say."""

    def __init__(self, message: str = "", *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LlmRateLimitError(LlmLimitError):
    """429 — retryable with backoff, for as long as the wait is short."""


class LlmQuotaError(LlmLimitError):
    """The account is out of credit or quota (OpenAI's `insufficient_quota`, a
    402 from a gateway, "usage limit reached"). Retrying at once cannot help, so
    it is not retryable; it can only end once the window resets (when the
    provider says when) or a person adds credit."""


class LlmConnectionError(LlmError):
    """Network-level failure reaching the provider. Retryable."""


class LlmTimeoutError(LlmConnectionError):
    """Request timed out. Retryable."""


class LlmUnreachableError(LlmConnectionError):
    """The provider could not be reached at all: the connection was never made
    (a connect timeout, a refused or reset connect, DNS, a proxy that would not
    pass it on), so nothing was sent and nothing was processed.

    Not an LlmTimeoutError, deliberately. A timeout means a call already spent
    its whole deadline and the provider likely stalled, so the loop allows one
    more try; this costs seconds and is usually brief (a proxy hiccup, a VPN
    dropping), so the loop waits it out for longer. When even that is not
    enough the run ends carrying a ProviderLimit of kind "unreachable" and the
    story is parked and retried later (elstin/services/provider_limit.py) instead
    of being failed for a network blip (two connects slower than the
    SDK's 5s default, twelve seconds apart, ended the story)."""


class LlmServerError(LlmError):
    """5xx from the provider. Retryable."""


# NB: a provider *refusal* is not an exception — it arrives as a normal
# 200 response carrying stop_reason="content_filter"
# (elstin/llm/types.py's StopReason), which elstin/agents/loop.py turns
# into a terminal, non-retryable loop failure. There is deliberately no
# LlmContentFilterError.

# How providers word "you have no credit/quota left", for an adapter whose SDK
# reports it as an ordinary 429 or 4xx rather than its own type.
_QUOTA_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"insufficient[_ ]quota",
        r"exceeded your current quota",
        r"insufficient (?:credits?|funds|balance)",
        r"(?:out of|no) credits?",
        r"credit balance is too low",
        r"billing[_ ](?:hard[_ ])?limit",
        r"(?:usage|spend(?:ing)?|monthly|weekly|daily) limit (?:reached|exceeded|has been reached)",
        r"reached (?:your|the|its) (?:\w+ ){0,2}(?:usage|spend(?:ing)?|billing) limit",
        r"payment required",
        r"requires more credits",
    )
)


def is_quota_message(message: str) -> bool:
    return any(p.search(message) for p in _QUOTA_PATTERNS)


_DURATION_UNITS = {
    "ms": 0.001, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|seconds?|secs?|minutes?|mins?|hours?|hrs?|h|m|s)(?![a-z])", re.IGNORECASE)
# "try again in 7m12s", "retry after 20 seconds", "resets in 1.5 hours".
_WAIT_IN_MESSAGE = re.compile(
    r"(?:try again|retry|resets?|available again|wait)\b[^.\n]{0,24}?\b(?:in|after|for)\s+"
    r"((?:\d+(?:\.\d+)?\s*(?:ms|seconds?|secs?|minutes?|mins?|hours?|hrs?|h|m|s)(?![a-z])[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)


def parse_duration_seconds(text: str) -> float | None:
    """`1h2m3s`, `7m12s`, `250ms`, `20 minutes`, `1.5 hours` -> seconds; None
    when nothing in `text` is a duration."""
    parts = _DURATION_PART.findall(text)
    if not parts:
        return None
    return sum(float(amount) * _DURATION_UNITS[unit.lower()] for amount, unit in parts)


def retry_after_seconds(headers: Mapping[str, str] | None, message: str = "") -> float | None:
    """When a provider says it will accept calls again, in seconds from now:
    `retry-after-ms`, then `retry-after` (seconds, or an HTTP date), then
    OpenAI's `x-ratelimit-reset-*` durations (the later of requests/tokens, since
    the call has to clear both), then the reset time the message itself names.
    None when none of them says."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    try:
        if "retry-after-ms" in lowered:
            return max(0.0, float(lowered["retry-after-ms"]) / 1000.0)
        if "retry-after" in lowered:
            raw = lowered["retry-after"].strip()
            try:
                return max(0.0, float(raw))
            except ValueError:
                return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        pass
    resets = [
        seconds
        for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")
        if name in lowered and (seconds := parse_duration_seconds(lowered[name])) is not None
    ]
    if resets:
        return max(resets)
    found = _WAIT_IN_MESSAGE.search(message or "")
    return parse_duration_seconds(found.group(1)) if found else None


# Past this a reset instant is a corrupt or unit-confused number, not a window
# worth parking a run on (the longest real ones are a provider's weekly limit).
MAX_LIMIT_WAIT_SECONDS = 7 * 24 * 3600
# Added to the provider's own figure: it is rounded, and a retry that lands a
# second early is refused and wastes the wait.
LIMIT_RESET_MARGIN_SECONDS = 5


@dataclass(frozen=True)
class ProviderLimit:
    """A model call that ended on a limit — or, kind "unreachable", on the
    provider not being reachable at all — in a form that survives being
    checkpointed in a run's state. `reset_at` is epoch seconds, or None when the
    provider did not say when the limit lifts — then only a person can tell. An
    unreachable provider never says: it always has None, and the retry time is
    elstin's own schedule (provider_limit.plan_parking)."""

    kind: str  # "rate" | "quota" | "unreachable"
    reset_at: float | None
    message: str = ""

    def as_state(self) -> dict:
        return {"kind": self.kind, "reset_at": self.reset_at, "message": self.message[:500]}


def provider_limit_of(exc: BaseException | None, *, now: float | None = None) -> ProviderLimit | None:
    """The limit `exc` reports, or None when it is some other failure. A reset
    further away than MAX_LIMIT_WAIT_SECONDS counts as unknown. A provider that
    could not be reached is reported too, as kind "unreachable"."""
    if isinstance(exc, LlmUnreachableError):
        return ProviderLimit(kind="unreachable", reset_at=None, message=str(exc))
    if not isinstance(exc, LlmLimitError):
        return None
    wait = exc.retry_after_seconds
    reset_at = None
    if wait is not None and 0 <= wait <= MAX_LIMIT_WAIT_SECONDS:
        reset_at = (time.time() if now is None else now) + wait + LIMIT_RESET_MARGIN_SECONDS
    return ProviderLimit(
        kind="quota" if isinstance(exc, LlmQuotaError) else "rate", reset_at=reset_at, message=str(exc),
    )


RETRYABLE_ERRORS: tuple[type[LlmError], ...] = (
    LlmRateLimitError,
    LlmConnectionError,
    LlmTimeoutError,
    LlmServerError,
)


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RETRYABLE_ERRORS)
