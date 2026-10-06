"""No database, no network — pure classification logic.

The cases below are the real PROJ-3480 epic: five implementation stories and
one validation story, all `issuetype: Story`, none labelled for purpose.
That epic is the whole reason this classifier exists, so it is the fixture:
if a change to the heuristic stops separating PROJ-3722 from its five
siblings, these fail.
"""
from __future__ import annotations

import pytest

from elstin.services.story_kind import KIND_DEV, KIND_QA, classify_story_kind

# Verbatim summaries from PROJ-3480's children.
_PROJ_3480_DEV_SUMMARIES = [
    "[Shop App] Select multiple item types on the search form",
    "[Shop App] Search and retrieve offers for a multi-item order",
    "[Shop App] View multi-item pricing on the results list and details",
    "[Shop App] Complete checkout for multiple item types through review and acknowledgement",
    "[Shop App] Handle multi-item errors and persist selections",
]

_PROJ_3722_SUMMARY = "[Shop App] Validate implemented end-to-end multi-item flow"
_PROJ_3722_DESCRIPTION = (
    "Validate implemented end-to-end multi-item flow and address identified gaps. "
    "Transition ownership of the Multi Item capability to the designated team and "
    "perform an independent end-to-end validation of the delivered functionality."
)


@pytest.mark.parametrize("summary", _PROJ_3480_DEV_SUMMARIES)
def test_proj_3480_implementation_stories_classify_as_dev(summary):
    kind, _reason = classify_story_kind(summary=summary, issue_type="Story", labels=[])
    assert kind == KIND_DEV


def test_proj_3722_validation_story_classifies_as_qa():
    kind, reason = classify_story_kind(
        summary=_PROJ_3722_SUMMARY,
        description=_PROJ_3722_DESCRIPTION,
        issue_type="Story",
        labels=["TeamAchievers"],  # a team label, not a purpose label
    )
    assert kind == KIND_QA
    assert reason == "lead_token:validate+scope_term"


def test_explicit_qa_label_outranks_prose():
    kind, reason = classify_story_kind(summary="Add a button", issue_type="Story", labels=["QA"])
    assert (kind, reason) == (KIND_QA, "label:qa")


def test_explicit_dev_label_outranks_qa_prose():
    """A human labelling a ticket `dev` overrides the heuristic even when
    the prose looks like validation work — the label is the deliberate
    signal, the prose is inference."""
    kind, reason = classify_story_kind(
        summary="Validate the end-to-end flow", issue_type="Story", labels=["implementation"]
    )
    assert (kind, reason) == (KIND_DEV, "label:implementation")


def test_qa_issue_type_classifies_as_qa():
    kind, _reason = classify_story_kind(summary="Anything at all", issue_type="Test Execution")
    assert kind == KIND_QA


def test_bug_issue_type_is_dev_not_qa():
    """A bug is implementation work — it gets fixed, not validated."""
    kind, _reason = classify_story_kind(summary="Crash on tapping View Prices", issue_type="Bug")
    assert kind == KIND_DEV


def test_validate_without_scope_term_stays_dev():
    """The classifier's most important negative case: "add validation to X"
    is an ordinary dev story that happens to open with the word Validate.
    Treating it as QA would skip implementing it entirely."""
    kind, reason = classify_story_kind(
        summary="Validate quantity input on the checkout form",
        description="Reject quantities below 1 and show an inline error.",
        issue_type="Story",
    )
    assert kind == KIND_DEV
    assert reason == "lead_token:validate_without_scope_term"


def test_lead_token_qa_is_strong_enough_alone():
    kind, reason = classify_story_kind(summary="QA sign-off for the checkout epic", issue_type="Story")
    assert (kind, reason) == (KIND_QA, "lead_token:qa")


def test_regression_lead_token_is_strong_enough_alone():
    kind, _reason = classify_story_kind(summary="Regression sweep across checkout flows", issue_type="Story")
    assert _reason == "lead_token:regression"


def test_bracket_prefixes_are_stripped_before_reading_the_lead_token():
    kind, _reason = classify_story_kind(
        summary="[Shop App][Checkout][P1] QA the whole thing", issue_type="Story"
    )
    assert kind == KIND_QA


def test_scope_term_in_description_alone_is_enough():
    kind, reason = classify_story_kind(
        summary="Verify the checkout capability",
        description="Full regression pass over the checkout journey before release.",
        issue_type="Story",
    )
    assert kind == KIND_QA
    assert reason == "lead_token:verify+scope_term"


def test_empty_summary_defaults_to_dev():
    assert classify_story_kind(summary="")[0] == KIND_DEV
