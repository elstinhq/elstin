"""Provider-agnostic exception hierarchy. forge/agents/loop.py catches
ONLY these — never a provider SDK's own exception classes — so adding a
second adapter never means touching the loop's retry logic.

Each adapter (forge/llm/openai_compat.py today) is responsible for
catching its own SDK's typed exceptions and re-raising the matching class
here — so "is this retryable?" is answered by the provider's own error
type, never by substring-matching an error message, which is what the
pre-migration code had to do when its only signal was a result string.
"""
from __future__ import annotations


class LlmError(RuntimeError):
    """Base class for every error a provider adapter can raise."""


class LlmAuthError(LlmError):
    """Missing or invalid credentials. Never retryable."""


class LlmBadRequestError(LlmError):
    """The request itself was rejected (bad schema, invalid params, ...).
    Never retryable without changing the request."""


class LlmRateLimitError(LlmError):
    """429 — retryable with backoff."""


class LlmConnectionError(LlmError):
    """Network-level failure reaching the provider. Retryable."""


class LlmTimeoutError(LlmConnectionError):
    """Request timed out. Retryable."""


class LlmServerError(LlmError):
    """5xx from the provider. Retryable."""


# NB: a provider *refusal* is not an exception — it arrives as a normal
# 200 response carrying stop_reason="content_filter"
# (forge/llm/types.py's StopReason), which forge/agents/loop.py turns
# into a terminal, non-retryable loop failure. There is deliberately no
# LlmContentFilterError.

RETRYABLE_ERRORS: tuple[type[LlmError], ...] = (
    LlmRateLimitError,
    LlmConnectionError,
    LlmTimeoutError,
    LlmServerError,
)


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RETRYABLE_ERRORS)
