"""The PO draft gate (elstin/agents/roles/po_gate.py): pure — no DB, no model.
Evidence is checked against real files in a tmp workspace and against what a
`PoResearch` says the agent read."""
from __future__ import annotations

import copy

import pytest

from elstin.agents.roles.plan_gate import blocking
from elstin.agents.roles.po_gate import (
    KIND_BAD_REF, KIND_DEPENDENCY_CYCLE, KIND_DEPENDS_ON_QA, KIND_DUPLICATE_STORY, KIND_EPIC_REPROPOSED,
    KIND_EVIDENCE_MISSING, KIND_EVIDENCE_QUOTE, KIND_EVIDENCE_UNREAD, KIND_EXISTING_COVERAGE, KIND_KIND_MISMATCH,
    KIND_LARGE_STORY, KIND_MALFORMED_OUTPUT, KIND_NO_CRITERIA, KIND_STORY_COUNT, KIND_UNKNOWN_DEPENDENCY,
    KIND_WRONG_MODE, DraftContext, carry_verification, check_draft, check_draft_integrity, check_structure,
    format_po_feedback, kind_warnings, verify_evidence,
)
from elstin.agents.roles.product_owner import PO_DRAFT_SCHEMA
from elstin.tools.po_tools import PoResearch
from elstin.tools.workspace import Workspace


def _story(ref="s1", **overrides):
    story = {
        "ref": ref, "summary": f"Do thing {ref}", "description": f"Why {ref} exists.",
        "acceptance_criteria": [f"{ref} behaves"], "issue_type": "Story", "kind": "dev", "labels": [],
        "depends_on": [], "subtasks": [], "evidence": [], "rationale": "", "size": "M",
    }
    story.update(overrides)
    return story


def _draft(mode="split", **overrides):
    draft = {
        "mode": mode,
        "epic": {
            "summary": "Retry payments", "description": "Retry failed payments.", "acceptance_criteria": ["Retries happen"],
            "existing_key": None if mode == "brainstorm" else "PAY-1", "suggested_description": None,
        },
        "stories": [_story("s1"), _story("s2")],
        "existing": [], "gaps": [], "assumptions": [], "out_of_scope": [], "questions": [], "reply": "ok",
    }
    draft.update(overrides)
    return draft


def _ctx(mode="split", **overrides):
    values = {"mode": mode, "epic_key": None if mode == "brainstorm" else "PAY-1", "epic_summary": "Retry payments", "max_stories": 15}
    values.update(overrides)
    return DraftContext(**values)


def _kinds(defects, *, only_blocking=False):
    return {d["kind"] for d in (blocking(defects) if only_blocking else defects)}


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "retry.py").write_text("def retry(payment):\n    # three attempts, then give up\n    return attempts(payment, 3)\n")
    # the same line twice, far apart: line 4 and line 25
    (root / "src" / "many.py").write_text("x = 1\n" * 3 + "needle = True\n" + "y = 2\n" * 20 + "needle = True\n")
    return Workspace(default_label="repo", roots={"repo": str(root)})


# -- the schema itself ------------------------------------------------------------------


def test_the_draft_schema_satisfies_strict_structured_output_rules():
    """Every property in `required` at every level, additionalProperties false:
    a schema that breaks this is rejected by the provider on a real run and by
    no test that fakes it."""

    def check(node, path="root"):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
            assert set(node["properties"]) == set(node["required"]), f"{path}: required != properties"
            for key, value in node["properties"].items():
                check(value, f"{path}.{key}")
        elif node.get("type") == "array":
            check(node.get("items", {}), f"{path}[]")

    check(PO_DRAFT_SCHEMA)


# -- integrity ----------------------------------------------------------------------------


def test_a_well_formed_draft_passes_integrity():
    assert check_draft_integrity(_draft(), mode="split") == []


def test_tool_call_markup_and_non_arrays_are_malformed():
    draft = _draft()
    draft["stories"][0]["description"] = 'text<parameter name="gaps">["x"]'
    draft["assumptions"] = '["a list written inside a string"]'
    defects = check_draft_integrity(draft, mode="split")
    assert _kinds(defects) == {KIND_MALFORMED_OUTPUT}
    reasons = " ".join(d["reason"] for d in defects)
    assert "tool-call markup" in reasons and "gaps" in reasons
    assert '"assumptions" must be a JSON array but is a string' in reasons


def test_a_story_field_that_is_not_an_array_is_malformed():
    draft = _draft()
    draft["stories"][1]["acceptance_criteria"] = "a single string"
    defects = check_draft_integrity(draft, mode="split")
    assert defects and defects[0]["where"] == "stories[1]"


def test_the_drafts_mode_must_be_the_sessions():
    defects = check_draft_integrity(_draft("gap"), mode="split")
    assert _kinds(defects) == {KIND_WRONG_MODE}


def test_an_answer_that_is_not_an_object_is_malformed():
    assert _kinds(check_draft_integrity("no", mode="split")) == {KIND_MALFORMED_OUTPUT}


# -- structure ------------------------------------------------------------------------------


def test_a_good_split_draft_has_no_blocking_defects():
    assert blocking(check_structure(_draft(), _ctx())) == []


def test_every_story_needs_criteria_summary_and_description():
    draft = _draft(stories=[_story("s1", acceptance_criteria=[]), _story("s2", description="", summary="TBD")])
    defects = check_structure(draft, _ctx())
    assert KIND_NO_CRITERIA in _kinds(defects, only_blocking=True)
    reasons = " ".join(d["reason"] for d in defects)
    assert "story s1 has no acceptance criteria" in reasons
    assert "story s2 has no summary" in reasons and "story s2 has no description" in reasons


def test_placeholder_criteria_do_not_count():
    defects = check_structure(_draft(stories=[_story("s1", acceptance_criteria=["TBD", "..."])]), _ctx())
    assert KIND_NO_CRITERIA in _kinds(defects, only_blocking=True)


def test_story_count_is_bounded_both_ways():
    too_many = _draft(stories=[_story(f"s{n}") for n in range(1, 18)])
    assert KIND_STORY_COUNT in _kinds(check_structure(too_many, _ctx(max_stories=15)), only_blocking=True)
    assert KIND_STORY_COUNT in _kinds(check_structure(_draft(stories=[]), _ctx()), only_blocking=True)
    assert KIND_STORY_COUNT in _kinds(check_structure(_draft("brainstorm", stories=[]), _ctx("brainstorm")), only_blocking=True)
    # Gap mode may find nothing new to propose.
    gap = _draft("gap", stories=[], existing=[])
    assert KIND_STORY_COUNT not in _kinds(check_structure(gap, _ctx("gap")))


def test_an_l_story_needs_a_rationale():
    defects = check_structure(_draft(stories=[_story("s1", size="L"), _story("s2", size="L", rationale="One migration touching every table; splitting it would leave the schema half-moved.")]), _ctx())
    large = [d for d in defects if d["kind"] == KIND_LARGE_STORY]
    assert len(large) == 1 and "story s1" in large[0]["reason"]


def test_refs_must_be_unique_labels_and_not_jira_keys():
    draft = _draft(stories=[_story("s1"), _story("s1", summary="Another"), _story("PAY-9", summary="Third")])
    defects = [d for d in check_structure(draft, _ctx()) if d["kind"] == KIND_BAD_REF]
    assert any("used by two stories" in d["reason"] for d in defects)
    assert any("looks like a JIRA key" in d["reason"] for d in defects)


def test_a_dependency_must_be_a_ref_or_a_known_ticket():
    draft = _draft(stories=[_story("s1", depends_on=["s2", "PAY-7", "ghost"]), _story("s2")])
    defects = check_structure(draft, _ctx(known_keys=frozenset({"PAY-7"})))
    unknown = [d for d in defects if d["kind"] == KIND_UNKNOWN_DEPENDENCY]
    assert len(unknown) == 1 and "'ghost'" in unknown[0]["reason"]


def test_a_dependency_cycle_is_named():
    draft = _draft(stories=[_story("s1", depends_on=["s3"]), _story("s2", depends_on=["s1"]), _story("s3", depends_on=["s2"])])
    cycles = [d for d in check_structure(draft, _ctx()) if d["kind"] == KIND_DEPENDENCY_CYCLE]
    assert len(cycles) == 1
    assert "s1 → s3 → s2 → s1" in cycles[0]["reason"] or "→" in cycles[0]["reason"]
    assert cycles[0]["blocking"] is True


def test_a_story_depending_on_itself_is_a_cycle():
    draft = _draft(stories=[_story("s1", depends_on=["s1"])])
    assert KIND_DEPENDENCY_CYCLE in _kinds(check_structure(draft, _ctx()), only_blocking=True)


def test_no_cycle_in_a_chain_or_a_diamond():
    draft = _draft(stories=[
        _story("s1"), _story("s2", depends_on=["s1"]), _story("s3", depends_on=["s1"]), _story("s4", depends_on=["s2", "s3"]),
    ])
    assert KIND_DEPENDENCY_CYCLE not in _kinds(check_structure(draft, _ctx()))


def test_a_dev_story_depending_on_a_qa_story_only_warns():
    draft = _draft(stories=[_story("s1", depends_on=["s2"]), _story("s2", kind="qa", summary="QA the flow")])
    defects = check_structure(draft, _ctx())
    qa = [d for d in defects if d["kind"] == KIND_DEPENDS_ON_QA]
    assert qa and qa[0]["blocking"] is False


def test_a_proposed_story_may_not_repeat_an_existing_one_or_the_epic_or_itself():
    ctx = _ctx("gap", existing=(("PAY-2", "Add backoff to retries"),))
    draft = _draft("gap", stories=[
        _story("s1", summary="add  BACKOFF to retries!"), _story("s2", summary="Retry payments"),
        _story("s3", summary="Notify the customer"), _story("s4", summary="notify the customer"),
    ], existing=[{"key": "PAY-2", "assessment": "done", "evidence": [], "suggested_edit": None}])
    duplicates = [d for d in check_structure(draft, ctx) if d["kind"] == KIND_DUPLICATE_STORY]
    reasons = " ".join(d["reason"] for d in duplicates)
    assert len(duplicates) == 3
    assert "existing story PAY-2" in reasons and "the epic PAY-1" in reasons and "story s3 of this draft" in reasons


def test_the_epic_is_never_proposed_as_new_in_split_or_gap():
    for mode in ("split", "gap"):
        draft = _draft(mode)
        draft["epic"]["existing_key"] = None
        ctx = _ctx(mode, existing=(("PAY-2", "x"),) if mode == "gap" else ())
        if mode == "gap":
            draft["existing"] = [{"key": "PAY-2", "assessment": "done", "evidence": [], "suggested_edit": None}]
        assert KIND_EPIC_REPROPOSED in _kinds(check_structure(draft, ctx), only_blocking=True)


def test_a_brainstorm_makes_a_new_epic_with_a_description_and_criteria():
    ok = check_structure(_draft("brainstorm"), _ctx("brainstorm"))
    assert blocking(ok) == []
    bad = _draft("brainstorm")
    bad["epic"].update(description="", acceptance_criteria=[], existing_key="PAY-1")
    kinds = _kinds(check_structure(bad, _ctx("brainstorm")), only_blocking=True)
    assert {KIND_EPIC_REPROPOSED, KIND_NO_CRITERIA} <= kinds


def test_gap_mode_assesses_every_existing_story_exactly_once():
    ctx = _ctx("gap", existing=(("PAY-2", "Backoff"), ("PAY-3", "Notify")))
    rows = [
        {"key": "PAY-2", "assessment": "done", "evidence": [], "suggested_edit": None},
        {"key": "PAY-2", "assessment": "partial", "evidence": [], "suggested_edit": None},
        {"key": "PAY-9", "assessment": "done", "evidence": [], "suggested_edit": None},
        {"key": "PAY-3", "assessment": "finished", "evidence": [], "suggested_edit": None},
    ]
    defects = check_structure(_draft("gap", stories=[], existing=rows), ctx)
    reasons = " ".join(d["reason"] for d in defects if d["kind"] == KIND_EXISTING_COVERAGE)
    assert "assessed twice" in reasons and "PAY-9" in reasons
    assert any("assessment must be one of" in d["reason"] for d in defects)

    missing = check_structure(_draft("gap", stories=[], existing=rows[:1]), ctx)
    assert any("missing: PAY-3" in d["reason"] for d in missing)


def test_existing_assessments_belong_to_gap_mode_only():
    rows = [{"key": "PAY-2", "assessment": "done", "evidence": [], "suggested_edit": None}]
    assert KIND_EXISTING_COVERAGE in _kinds(check_structure(_draft("split", existing=rows), _ctx("split")), only_blocking=True)


def test_questions_are_checked_for_stubs_and_unique_ids():
    stub = {"id": "q1", "question": "TBD", "context": None, "options": [{"id": "A", "label": "x", "description": None}]}
    twin = {"id": "q1", "question": "Real question?", "context": None, "options": []}
    defects = check_structure(_draft(questions=[stub, twin]), _ctx())
    reasons = " ".join(d["reason"] for d in defects)
    assert "stub text" in reasons and "its own non-empty id" in reasons


# -- kind advisory ------------------------------------------------------------------------------


def test_a_kind_elstin_would_classify_differently_is_a_warning():
    draft = _draft(stories=[
        _story("s1", summary="Validate end-to-end checkout flow", description="Regression sign-off", kind="dev"),
        _story("s2", summary="Add the endpoint", kind="qa"),
        _story("s3", summary="Validate end-to-end checkout flow", description="Regression sign-off", kind="qa"),
    ])
    warnings = kind_warnings(draft)
    assert [w["where"] for w in warnings] == ["stories[0].kind"]
    assert warnings[0]["blocking"] is False and "classify it qa" in warnings[0]["reason"]


def test_a_qa_story_is_judged_as_it_will_be_labelled_at_publish():
    # kind qa gets a `qa` label when published, so prose that reads like dev work is fine.
    assert kind_warnings(_draft(stories=[_story("s1", summary="Check the thing", kind="qa")])) == []


# -- evidence -----------------------------------------------------------------------------------------


def _with_evidence(*items):
    return _draft(stories=[_story("s1", evidence=list(items))])


def test_code_evidence_resolves_a_file_and_a_verbatim_quote(workspace):
    research = PoResearch()
    good = {"source": "code", "ref": "src/retry.py:2", "quote": "three attempts,\n    then give up"}
    annotated, defects = verify_evidence(_with_evidence(good), research, workspace)
    assert defects == []
    assert annotated["stories"][0]["evidence"][0]["verified"] is True
    assert "verified" not in good  # the caller's draft is untouched


def test_a_real_quote_at_the_wrong_line_is_cited_at_the_right_one(workspace):
    """The agent counts lines badly; the quote is the proof, the line is only how a
    person gets to it — so the line is corrected, not held against the draft."""
    items = [
        {"source": "code", "ref": "src/retry.py:1", "quote": "return attempts(payment, 3)"},
        {"source": "code", "ref": "src/retry.py:3", "quote": "return attempts(payment, 3)"},
        {"source": "code", "ref": "src/retry.py:1-3", "quote": "return attempts(payment, 3)"},
        {"source": "code", "ref": "src/retry.py", "quote": "return attempts(payment, 3)"},
        {"source": "code", "ref": "src/retry.py:1", "quote": "three attempts,\n   then give up"},
        {"source": "code", "ref": "src/many.py:20", "quote": "needle = True"},
        {"source": "code", "ref": "src/many.py:2", "quote": "needle = True"},
    ]
    annotated, defects = verify_evidence(_with_evidence(*items), PoResearch(), workspace)
    assert defects == []
    refs = [e["ref"] for e in annotated["stories"][0]["evidence"]]
    assert refs == [
        "src/retry.py:3",   # was 1: corrected
        "src/retry.py:3",   # right already
        "src/retry.py:1-3",  # a range that covers it is left as written
        "src/retry.py",     # no line was claimed, none is invented
        "src/retry.py:2",   # a quote that spans lines is placed where it starts
        "src/many.py:25",   # twice in the file: the one nearest the line cited
        "src/many.py:4",
    ]
    assert all(e["verified"] is True for e in annotated["stories"][0]["evidence"])
    assert items[0]["ref"] == "src/retry.py:1"  # the caller's draft is untouched


def test_code_evidence_failures_say_what_is_wrong(workspace):
    items = [
        {"source": "code", "ref": "src/missing.py", "quote": "x"},
        {"source": "code", "ref": "src/retry.py", "quote": "never written here"},
        {"source": "code", "ref": "src/retry.py:99", "quote": "def retry"},
        {"source": "code", "ref": "../../etc/passwd", "quote": "root"},
        {"source": "code", "ref": "@nope/x.py", "quote": "x"},
        {"source": "code", "ref": "src/retry.py", "quote": "  "},
    ]
    annotated, defects = verify_evidence(_with_evidence(*items), PoResearch(), workspace)
    assert [e["verified"] for e in annotated["stories"][0]["evidence"]] == [False] * 6
    kinds = [d["kind"] for d in defects]
    assert kinds == [
        KIND_EVIDENCE_MISSING, KIND_EVIDENCE_QUOTE, KIND_EVIDENCE_MISSING, KIND_EVIDENCE_MISSING, KIND_EVIDENCE_MISSING,
        KIND_EVIDENCE_QUOTE,
    ]
    assert "not a file in the workspace" in defects[0]["reason"]
    assert "not in src/retry.py" in defects[1]["reason"]
    assert "has only 3 line(s)" in defects[2]["reason"]
    assert all(d["where"].startswith("stories[0].evidence[") for d in defects)


def test_jira_and_confluence_evidence_must_have_been_read(workspace):
    research = PoResearch()
    research.saw_jira("PAY-14", "PAY-14 — Retries\nStatus: Done\nFailed payments are retried three times.")
    research.saw_page("222", "Retry design [ENG] (page 222)\n\nBackoff is 1s, 5s, 30s.")
    items = [
        {"source": "jira", "ref": "pay-14", "quote": "retried three times"},
        {"source": "jira", "ref": "PAY-15", "quote": "anything"},
        {"source": "jira", "ref": "PAY-14", "quote": "retried five times"},
        {"source": "confluence", "ref": "https://acme.atlassian.net/wiki/spaces/ENG/pages/222/Retry", "quote": "1s, 5s, 30s"},
        {"source": "confluence", "ref": "333", "quote": "x"},
        {"source": "confluence", "ref": "222", "quote": "Backoff is a minute"},
        {"source": "wiki", "ref": "x", "quote": "x"},
    ]
    annotated, defects = verify_evidence(_with_evidence(*items), research, workspace)
    assert [e["verified"] for e in annotated["stories"][0]["evidence"]] == [True, False, False, True, False, False, False]
    assert [d["kind"] for d in defects] == [
        KIND_EVIDENCE_UNREAD, KIND_EVIDENCE_QUOTE, KIND_EVIDENCE_UNREAD, KIND_EVIDENCE_QUOTE, KIND_EVIDENCE_MISSING,
    ]
    assert "did not read this session" in defects[0]["reason"]


def test_existing_story_evidence_is_checked_too(workspace):
    draft = _draft("gap", stories=[], existing=[{
        "key": "PAY-2", "assessment": "done", "suggested_edit": None,
        "evidence": [{"source": "code", "ref": "src/retry.py", "quote": "not there"}],
    }])
    _annotated, defects = verify_evidence(draft, PoResearch(), workspace)
    assert defects and defects[0]["where"] == "existing[0].evidence[0]"


def test_an_edited_draft_keeps_the_marks_on_evidence_it_did_not_touch():
    old = _with_evidence(
        {"source": "code", "ref": "a.py", "quote": "one", "verified": True},
        {"source": "code", "ref": "b.py", "quote": "two", "verified": False},
    )
    edited = _with_evidence(
        {"source": "code", "ref": "a.py", "quote": "one"},
        {"source": "code", "ref": "b.py", "quote": "two", "verified": True},  # a client cannot vouch for itself
        {"source": "code", "ref": "c.py", "quote": "new"},
    )
    kept = carry_verification(edited, old)["stories"][0]["evidence"]
    assert [e.get("verified") for e in kept] == [True, False, None]


# -- the whole turn -------------------------------------------------------------------------------------


def test_check_draft_returns_the_marked_draft_and_everything_found(workspace):
    draft = _draft(stories=[_story("s1", acceptance_criteria=[], evidence=[{"source": "code", "ref": "src/nope.py", "quote": "x"}])])
    annotated, defects = check_draft(draft, _ctx(), PoResearch(), workspace)
    kinds = _kinds(defects)
    assert {KIND_NO_CRITERIA, KIND_EVIDENCE_MISSING} <= kinds
    assert annotated["stories"][0]["evidence"][0]["verified"] is False


def test_a_malformed_answer_is_judged_on_that_alone(workspace):
    draft = _draft()
    draft["gaps"] = "not a list"
    draft["stories"][0]["acceptance_criteria"] = []
    _annotated, defects = check_draft(draft, _ctx(), PoResearch(), workspace)
    assert _kinds(defects) == {KIND_MALFORMED_OUTPUT}


def test_a_clean_draft_passes_the_whole_gate(workspace):
    draft = _draft(stories=[_story("s1", evidence=[{"source": "code", "ref": "src/retry.py", "quote": "def retry(payment):"}]), _story("s2", depends_on=["s1"])])
    _annotated, defects = check_draft(draft, _ctx(), PoResearch(), workspace)
    assert blocking(defects) == []


# -- the correction sent back ---------------------------------------------------------------------------


def test_the_feedback_names_each_failing_check_and_says_what_to_do(workspace):
    draft = _draft(stories=[
        _story("s1", acceptance_criteria=[], depends_on=["s2"], evidence=[{"source": "jira", "ref": "PAY-99", "quote": "x"}]),
        _story("s2", depends_on=["s1"]),
    ])
    _annotated, defects = check_draft(draft, _ctx(), PoResearch(), workspace)
    feedback = format_po_feedback(defects, attempt=1, max_attempts=3)
    assert "YOUR PREVIOUS DRAFT WAS REJECTED" in feedback and "attempt 1/3" in feedback
    assert "no acceptance criteria" in feedback
    assert "loop (s1 → s2 → s1)" in feedback or "depend on each other in a loop" in feedback
    assert "you did not read this session" in feedback and "jira_get_issue" in feedback
    assert "Fix these in the draft and answer again with the whole draft" in feedback


def test_a_malformed_answers_feedback_is_about_the_answer():
    draft = _draft()
    draft["gaps"] = "x"
    feedback = format_po_feedback(check_draft_integrity(draft, mode="split"), attempt=1, max_attempts=3)
    assert "not a well-formed draft object" in feedback
    assert "Answer again: one draft object" in feedback


def test_defects_over_the_listing_cap_are_counted_not_listed():
    draft = _draft(stories=[_story(f"s{n}", acceptance_criteria=[]) for n in range(1, 21)])
    feedback = format_po_feedback(check_structure(draft, _ctx(max_stories=30)), attempt=1, max_attempts=3)
    assert "... and 8 more" in feedback


def test_the_gate_leaves_the_callers_draft_alone(workspace):
    draft = _draft(stories=[_story("s1", evidence=[{"source": "code", "ref": "src/retry.py", "quote": "def retry(payment):"}])])
    before = copy.deepcopy(draft)
    check_draft(draft, _ctx(), PoResearch(), workspace)
    assert draft == before


# -- bug mode --------------------------------------------------------------------------------


def _bug(**overrides):
    bug = {
        "summary": "Report shows the list price, not the sale price", "observed": "List price shown.",
        "expected": "Sale price shown.", "steps_to_reproduce": ["Sell a unit below list"],
        "root_cause": "The report reads the list price.", "confidence": "high", "cause_in": "code",
        "fix_location": "src/retry.py — read the sale price.", "to_confirm": [],
        "acceptance_criteria": ["The report shows the sale price; a test covers a unit sold below list"],
        "labels": [], "evidence": [{"source": "code", "ref": "src/retry.py:3", "quote": "return attempts(payment, 3)"}],
    }
    bug.update(overrides)
    return bug


def _bug_draft(**overrides):
    draft = {"mode": "bug", "bug": _bug(), "assumptions": [], "out_of_scope": [], "questions": [], "reply": "Found it"}
    draft.update(overrides)
    return draft


def test_the_bug_schema_satisfies_strict_structured_output_rules():
    from elstin.agents.roles.product_owner import BUG_DRAFT_SCHEMA, draft_schema

    def check(node, path="root"):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
            assert set(node["properties"]) == set(node["required"]), f"{path}: required != properties"
            for key, value in node["properties"].items():
                check(value, f"{path}.{key}")
        elif node.get("type") == "array":
            check(node.get("items", {}), f"{path}[]")

    check(BUG_DRAFT_SCHEMA)
    assert draft_schema("bug") is BUG_DRAFT_SCHEMA and draft_schema("gap") is PO_DRAFT_SCHEMA


def test_a_bug_draft_is_judged_as_a_bug_not_as_a_backlog():
    """No epic, no stories — and neither is asked for."""
    assert check_draft_integrity(_bug_draft(), mode="bug") == []
    assert check_structure(_bug_draft(), _ctx("bug", epic_key=None)) == []


def test_a_bug_draft_must_be_one_and_in_the_sessions_mode():
    assert _kinds(check_draft_integrity(_bug_draft(bug="a string"), mode="bug")) == {KIND_MALFORMED_OUTPUT}
    assert KIND_MALFORMED_OUTPUT in _kinds(check_draft_integrity(_bug_draft(bug=_bug(evidence="[]")), mode="bug"))
    assert _kinds(check_draft_integrity(_draft("split"), mode="bug")) == {KIND_MALFORMED_OUTPUT, KIND_WRONG_MODE}


def test_a_bug_needs_what_goes_wrong_why_where_and_criteria():
    from elstin.agents.roles.po_gate import KIND_STUB_DRAFT, KIND_STUB_FIELD

    draft = _bug_draft(bug=_bug(summary="", observed=" ", root_cause="TODO", fix_location="", acceptance_criteria=[], confidence="sure"))
    defects = check_structure(draft, _ctx("bug"))
    where = {d["where"] for d in blocking(defects)}
    assert {"bug.summary", "bug.observed", "bug.root_cause", "bug.fix_location", "bug.acceptance_criteria", "bug.confidence"} <= where
    assert {KIND_STUB_DRAFT, KIND_STUB_FIELD, KIND_NO_CRITERIA} <= _kinds(defects)


def test_a_confident_cause_in_the_code_must_cite_the_code():
    from elstin.agents.roles.po_gate import KIND_UNSUPPORTED_CAUSE

    jira_only = [{"source": "jira", "ref": "PAY-9", "quote": "price is wrong"}]
    for confidence in ("high", "medium"):
        defects = check_structure(_bug_draft(bug=_bug(confidence=confidence, evidence=jira_only)), _ctx("bug"))
        assert KIND_UNSUPPORTED_CAUSE in _kinds(defects, only_blocking=True)
    # A low-confidence cause is a pointer, not a claim; a cause outside the code has no code to cite.
    assert check_structure(_bug_draft(bug=_bug(confidence="low", evidence=[])), _ctx("bug")) == []
    assert blocking(check_structure(_bug_draft(bug=_bug(cause_in="data", evidence=[])), _ctx("bug"))) == []


def test_a_cause_outside_the_code_is_a_warning_since_elstin_can_only_change_code():
    from elstin.agents.roles.po_gate import KIND_CAUSE_NOT_IN_CODE

    for place in ("data", "configuration"):
        defects = check_structure(_bug_draft(bug=_bug(cause_in=place)), _ctx("bug"))
        assert _kinds(defects) == {KIND_CAUSE_NOT_IN_CODE} and blocking(defects) == []
    assert check_structure(_bug_draft(bug=_bug(cause_in="unknown", confidence="low")), _ctx("bug")) == []


def test_a_bugs_evidence_is_verified_like_a_storys(workspace):
    bad = {"source": "code", "ref": "src/retry.py", "quote": "return the sale price"}
    annotated, defects = verify_evidence(_bug_draft(bug=_bug(evidence=[_bug()["evidence"][0], bad])), PoResearch(), workspace)
    assert [e["verified"] for e in annotated["bug"]["evidence"]] == [True, False]
    assert [(d["kind"], d["where"]) for d in defects] == [(KIND_EVIDENCE_QUOTE, "bug.evidence[1]")]
    assert defects[0]["reason"].startswith("the bug: evidence src/retry.py")


def test_a_bug_draft_passes_the_whole_gate(workspace):
    annotated, defects = check_draft(_bug_draft(), _ctx("bug"), PoResearch(), workspace)
    assert defects == [] and annotated["bug"]["evidence"][0]["verified"] is True


def test_an_edited_bug_keeps_the_marks_on_evidence_it_did_not_touch():
    old = _bug_draft(bug=_bug(evidence=[{**_bug()["evidence"][0], "verified": True}]))
    new = copy.deepcopy(_bug_draft())
    new["bug"]["evidence"].append({"source": "code", "ref": "src/x.py", "quote": "new", "verified": True})
    merged = carry_verification(new, old)
    assert merged["bug"]["evidence"][0]["verified"] is True and "verified" not in merged["bug"]["evidence"][1]


def test_a_bug_labelled_as_qa_would_be_reviewed_not_fixed_and_says_so():
    assert kind_warnings(_bug_draft()) == []
    warnings = kind_warnings(_bug_draft(bug=_bug(labels=["qa"])))
    assert _kinds(warnings) == {KIND_KIND_MISMATCH} and warnings[0]["where"] == "bug.labels"


def test_the_feedback_for_an_unsupported_cause_says_what_to_do():
    from elstin.agents.roles.po_gate import KIND_UNSUPPORTED_CAUSE

    defects = check_structure(_bug_draft(bug=_bug(evidence=[])), _ctx("bug"))
    text = format_po_feedback(defects, attempt=1, max_attempts=3)
    assert "without the code that shows it" in text and "quote the lines" in text
    assert _kinds(defects) == {KIND_UNSUPPORTED_CAUSE}
