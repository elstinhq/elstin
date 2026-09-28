"""Provider-agnostic exception hierarchy. forge/agents/loop.py catches
ONLY these — never a provider SDK's own exception classes — so adding a
second adapter never means touching the loop's retry logic.

Each adapter (forge/llm/openai_compat.py today) is responsible for
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


class LlmError(RuntimeError):
    """Base class for every error a provider adapter can raise."""


class LlmAuthError(LlmError):
    """Missing or invalid credentials. Never retryable."""


class LlmBadRequestError(LlmError):
    """The request itself was rejected (bad schema, invalid params, ...).
    Never retryable without changing the request."""


class LlmContextOverflowError(LlmBadRequestError):
    """The prompt no longer fits the model's context window. Not retryable
    as-is — but forge/agents/loop.py can shrink the history and send the
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
