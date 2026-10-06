"""Classifies a synced JIRA ticket as implementation work ("dev") or
validation work ("qa").

Why this is a heuristic rather than a field lookup: in a real JIRA project
a validation ticket is usually not structurally distinguishable from an
implementation ticket. PROJ-3722 ("Validate implemented end-to-end
multi-item flow") and PROJ-3481 ("Select multiple item types on
the search form") are both `issuetype: Story`, both unlabelled for
purpose, both children of the same epic. The only thing separating them is
what the summary and description *say*.

So the contract here is: **the heuristic proposes, a human disposes.** The
classification is computed once at sync time, persisted on `Story.kind`,
and overridable via `PUT /api/stories/{key}/kind`. It is never recomputed
behind the user's back, because silently reclassifying a story a human
already corrected would be worse than being wrong once.

The heuristic is deliberately biased toward "dev" — a dev story wrongly
routed to QA produces no code at all and stalls the epic, whereas a QA
story wrongly routed to dev produces a plan-and-implement pass over a
ticket that asks for validation, which the verifier gate still constrains.
Missing a QA ticket is recoverable; inventing one is not. Every rule below
therefore requires an explicit, unambiguous signal.
"""
from __future__ import annotations

import re

StoryKind = str  # "dev" | "qa"

KIND_DEV: StoryKind = "dev"
KIND_QA: StoryKind = "qa"

# An explicit JIRA label always wins — it's the one signal a human set on
# purpose, so it outranks anything inferred from prose.
_QA_LABELS = frozenset({"qa", "qa-only", "validation", "verification", "e2e", "uat", "regression", "testing"})
_DEV_LABELS = frozenset({"dev", "development", "implementation", "feature"})

# Issue types some JIRA projects define for validation work. `Bug` is
# deliberately absent: a bug ticket is implementation work.
_QA_ISSUE_TYPES = frozenset({"qa", "qa task", "test", "test case", "test execution", "validation"})

# Leading tokens strong enough on their own — a ticket whose summary opens
# with "QA" or "Regression" is not an implementation ticket in any project.
_QA_LEAD_TOKENS_STRONG = frozenset({"qa", "regression"})

# Leading tokens that need corroboration. "Validate the quantity field" is a
# perfectly ordinary dev story; "Validate the implemented end-to-end flow"
# is not. The lead verb alone cannot tell those apart — a scope term must
# also be present.
_QA_LEAD_TOKENS_WEAK = frozenset({"validate", "validation", "verify", "verification", "test", "testing"})

# Terms that mark work as whole-feature validation rather than a unit of
# implementation.
_QA_SCOPE_TERMS = (
    "end-to-end",
    "end to end",
    "e2e",
    "regression",
    "test plan",
    "test cases",
    "test scenarios",
    "sign-off",
    "signoff",
    "sign off",
    "uat",
    "acceptance testing",
    "independent validation",
    "qa validation",
    "smoke test",
    "exploratory testing",
)

# Strips JIRA's conventional bracketed prefixes ("[Shop App][Search] ...")
# so the lead token is the actual first word of the summary.
_BRACKET_PREFIX_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
_LEAD_TOKEN_RE = re.compile(r"[a-z]+")


def _lead_token(summary: str) -> str:
    stripped = _BRACKET_PREFIX_RE.sub("", summary or "").lower()
    match = _LEAD_TOKEN_RE.match(stripped)
    return match.group(0) if match else ""


def _has_scope_term(text: str) -> bool:
    return any(term in text for term in _QA_SCOPE_TERMS)


def classify_story_kind(
    *,
    summary: str,
    description: str | None = None,
    issue_type: str | None = None,
    labels: list[str] | None = None,
) -> tuple[StoryKind, str]:
    """Returns `(kind, reason)`. The reason is a short machine-ish string
    naming the rule that fired — it gets logged at sync time and shown next
    to the classification, so a human deciding whether to override can see
    *why* elstin guessed what it did rather than having to re-derive it."""
    normalized_labels = {(label or "").strip().lower() for label in (labels or [])}

    if normalized_labels & _QA_LABELS:
        return KIND_QA, f"label:{sorted(normalized_labels & _QA_LABELS)[0]}"
    if normalized_labels & _DEV_LABELS:
        return KIND_DEV, f"label:{sorted(normalized_labels & _DEV_LABELS)[0]}"

    if (issue_type or "").strip().lower() in _QA_ISSUE_TYPES:
        return KIND_QA, f"issue_type:{issue_type}"

    lead = _lead_token(summary)
    if lead in _QA_LEAD_TOKENS_STRONG:
        return KIND_QA, f"lead_token:{lead}"

    if lead in _QA_LEAD_TOKENS_WEAK:
        haystack = f"{summary or ''}\n{description or ''}".lower()
        if _has_scope_term(haystack):
            return KIND_QA, f"lead_token:{lead}+scope_term"
        # Lead verb without a scope term — almost always "add validation to
        # X", which is implementation work.
        return KIND_DEV, f"lead_token:{lead}_without_scope_term"

    return KIND_DEV, "default"
