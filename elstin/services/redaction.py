"""Scrubbing credentials out of what a run leaves behind.

An agent's tool output is not curated: a command that prints its environment, a
file read that turns out to be a `.env`, a build log that echoes a registry
token. elstin persists that output (the activity feed, which the UI streams and
re-reads) and, when tracing is on, sends it to LangSmith. Both are places a
credential should never land, so both pass through `redact` first.

Two complementary strategies, applied in this order:

1. **By value** — the exact values elstin itself holds: every secret setting
   (the model key, the JIRA and GitHub tokens, a project's own overrides, ...)
   and the values of the host's secret-named environment variables. This is the
   precise one: it catches a credential with no recognisable shape.
2. **By shape** — well-known token formats (GitHub, OpenAI/OpenRouter,
   Anthropic, AWS, Google, Slack, GitLab, Atlassian, LangSmith, npm, JWTs,
   private-key blocks, `Authorization: Bearer ...`, and the password in a
   `scheme://user:password@host` URL), for secrets elstin never held.

A value shorter than `MIN_SECRET_LEN` is not scrubbed by value: a short
"secret" is usually a common word, and replacing it everywhere would damage
ordinary output (a `POSTGRES_PASSWORD=postgres` turns `postgresql-16` into
`[REDACTED]ql-16`). Shape-based redaction does not have that problem and still
catches short-but-real tokens.

Best effort, not a guarantee: a credential that is neither a value elstin holds
nor a known shape gets through. Nothing here may fail a run — a settings read
that errors falls back to shapes alone.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Iterable
from typing import Any

from elstin.tools.agent_env import NEVER_FORWARD, SECRET_NAME_NOT_A_SECRET, is_secret_name

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 12

# How long the set of secret values is reused. Reading it is two small queries,
# and an activity event is written on every agent turn; a secret saved at
# /settings is covered within this many seconds.
_VALUES_TTL_SECONDS = 30.0

# Past this depth a container is rendered to text and scrubbed as one string,
# rather than walked: bounded recursion that still never lets a value through.
_MAX_DEPTH = 24

_TOKEN_SHAPES: tuple[str, ...] = (
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",  # GitHub PAT / OAuth / server / user / refresh
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",  # GitHub fine-grained PAT
    r"\bsk-ant-[A-Za-z0-9_-]{20,}",  # Anthropic
    r"\bsk-[A-Za-z0-9_-]{20,}",  # OpenAI, OpenRouter and compatible
    r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",  # Stripe secret / restricted
    r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key id
    r"\bASIA[0-9A-Z]{16}\b",  # AWS temporary access key id
    r"\bAIza[0-9A-Za-z_-]{35}",  # Google API key
    r"\bya29\.[0-9A-Za-z_-]+",  # Google OAuth access token
    r"\bxox[baprs]-[0-9A-Za-z-]{10,}",  # Slack
    r"\bglpat-[0-9A-Za-z_-]{20,}",  # GitLab PAT
    r"\bATATT3[A-Za-z0-9_=-]{20,}",  # Atlassian API token
    r"\blsv2_[a-z]{2}_[A-Za-z0-9_]{20,}",  # LangSmith
    r"\bnpm_[A-Za-z0-9]{30,}",  # npm
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # a JWT
)
# One alternation, so a large text (a model call's whole history goes through
# here on every turn when tracing is on) is scanned once rather than once per
# format.
_TOKEN_RE = re.compile("|".join(_TOKEN_SHAPES))
_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
# A block cut off before its END line (activity detail is truncated).
_PRIVATE_KEY_UNTERMINATED = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.DOTALL)
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=-]{16,}")
_URL_PASSWORD = re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)@")

_lock = threading.Lock()
_cached: tuple[float, frozenset[str]] | None = None


def environment_secret_values(environ: dict[str, str] | None = None) -> set[str]:
    """Values of the host's own credential-named variables (and
    `DATABASE_URL`) — what a command that prints its environment would show."""
    source = os.environ if environ is None else environ
    return {
        value.strip()
        for name, value in source.items()
        if value
        and (name in NEVER_FORWARD or (is_secret_name(name) and name not in SECRET_NAME_NOT_A_SECRET))
    }


def _gather_values() -> frozenset[str]:
    values = environment_secret_values()
    try:
        from elstin.config.store import secret_values

        values |= secret_values()
    except Exception:  # noqa: BLE001 - shapes alone still apply; see module docstring
        logger.debug("could not read secret settings for redaction", exc_info=True)
    return frozenset(v for v in values if len(v) >= MIN_SECRET_LEN)


def secret_values() -> frozenset[str]:
    """What `redact` scrubs by value, reused for a short while."""
    global _cached
    now = time.monotonic()
    with _lock:
        if _cached is not None and now - _cached[0] < _VALUES_TTL_SECONDS:
            return _cached[1]
    gathered = _gather_values()
    with _lock:
        _cached = (now, gathered)
    return gathered


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None


def redact(text: str, *, values: Iterable[str] | None = None) -> str:
    """`text` with every credential replaced by `[REDACTED]`. `values` replaces
    the exact values to scrub (default: what elstin holds); shapes always apply."""
    if not text:
        return text
    exact = secret_values() if values is None else frozenset(v for v in values if len(v) >= MIN_SECRET_LEN)
    # Longest first, so a value that contains another is scrubbed whole.
    for value in sorted(exact, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    text = _PRIVATE_KEY_BLOCK.sub(REDACTED, text)
    text = _PRIVATE_KEY_UNTERMINATED.sub(REDACTED, text)
    text = _TOKEN_RE.sub(REDACTED, text)
    text = _BEARER.sub(rf"\1\2{REDACTED}", text)
    return _URL_PASSWORD.sub(rf"\1:{REDACTED}@", text)


def redact_value(value: Any, *, values: Iterable[str] | None = None, _depth: int = 0) -> Any:
    """`redact` over every string inside a JSON-like value (dict values, list
    items, nested). Keys, numbers and other types are left as they are. Doubles
    as the LangSmith client's anonymizer, which takes one argument."""
    if isinstance(value, str):
        return redact(value, values=values)
    if isinstance(value, dict):
        if _depth >= _MAX_DEPTH:
            return redact(str(value), values=values)
        return {key: redact_value(item, values=values, _depth=_depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if _depth >= _MAX_DEPTH:
            return redact(str(value), values=values)
        return [redact_value(item, values=values, _depth=_depth + 1) for item in value]
    return value
