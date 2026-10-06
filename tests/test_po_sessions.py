"""PO sessions' pure logic (elstin/services/po_sessions.py): the mode a session
gets, what a person's edit changed, what is acceptable to save, and the draft as
text. No DB except where `inspect_epic` reads the project's ticket source."""
from __future__ import annotations

import copy

import pytest

import elstin.services.po_sessions as po_sessions
from elstin.services.jira_service import JiraTicket
from elstin.services.po_sessions import (
    PoModeError, PoSourceUnavailable, build_context, diff_paths, draft_for_storage, draft_to_markdown, jira_project_key_of,
    keep_published, publish_counts, published_edit_conflicts, render_epic_context, resolve_jira_project_key, select_mode,
    story_markdown, structure_problems, top_level_stories, validate_draft_edit,
)
from elstin.services.jira_service import JiraService


def _ticket(key, *, issue_type="Story", parent_key=None, summary=None, description="", status="To Do") -> JiraTicket:
    return JiraTicket(
        key=key, summary=summary or f"{key} summary", description=description, status=status, issue_type=issue_type,
        priority=None, parent_key=parent_key,
    )


EPIC = _ticket("PAY-1", issue_type="Epic", summary="Retry payments", description="Retry failed payments.")


# -- the mode --------------------------------------------------------------------------------


def test_no_epic_is_a_brainstorm():
    assert select_mode(None, []) == "brainstorm"
    assert select_mode("", []) == "brainstorm"


def test_an_epic_with_no_stories_is_split():
    assert select_mode("PAY-1", [EPIC]) == "split"


def test_an_epic_with_stories_is_gap():
    assert select_mode("PAY-1", [EPIC, _ticket("PAY-2", parent_key="PAY-1")]) == "gap"
    # A story linked through the legacy Epic Link reports no parent: still the epic's.
    assert select_mode("PAY-1", [EPIC, _ticket("PAY-3")]) == "gap"


def test_sub_tasks_alone_do_not_make_an_epic_a_gap():
    # A sub-task whose parent is not in the tree is treated as top-level by
    # elstin's own rule, so it is what makes the epic "have stories".
    subtask = _ticket("PAY-4", issue_type="Sub-task", parent_key="PAY-2")
    assert select_mode("PAY-1", [EPIC, _ticket("PAY-2", parent_key="PAY-1"), subtask]) == "gap"


def test_a_lone_story_with_no_children_is_split():
    story = _ticket("PAY-9", issue_type="Story")
    assert select_mode("PAY-9", [story]) == "split"


def test_an_epic_missing_from_its_tree_is_an_error():
    with pytest.raises(PoModeError, match="not in the ticket tree"):
        select_mode("PAY-1", [_ticket("PAY-2")])


def test_top_level_stories_are_what_elstin_would_dispatch():
    tree = [EPIC, _ticket("PAY-2", parent_key="PAY-1"), _ticket("PAY-3", parent_key="PAY-2", issue_type="Sub-task"), _ticket("PAY-4")]
    assert [t.key for t in top_level_stories("PAY-1", tree)] == ["PAY-2", "PAY-4"]
    assert top_level_stories("PAY-1", []) == []


# -- keys and context -----------------------------------------------------------------------------


def test_the_jira_project_key_comes_from_what_was_typed_then_the_epic_then_the_filter():
    assert jira_project_key_of("MCS2-857") == "MCS2"
    assert jira_project_key_of("gh.acme.app.12") is None
    assert resolve_jira_project_key(" pay ", "MCS2-857", {"jira_project_key": "X"}) == "PAY"
    assert resolve_jira_project_key(None, "MCS2-857", {"jira_project_key": "X"}) == "MCS2"
    assert resolve_jira_project_key(None, None, {"jira_project_key": "x"}) == "X"
    assert resolve_jira_project_key(None, None, None) is None


def test_the_context_records_what_a_draft_is_checked_against():
    tree = [EPIC, _ticket("PAY-2", parent_key="PAY-1", summary="Backoff")]
    context = build_context("PAY-1", tree, {"OTHER-7"})
    assert context == {
        "epic_summary": "Retry payments", "existing": [["PAY-2", "Backoff"]], "known_keys": ["OTHER-7", "PAY-1", "PAY-2"],
    }
    assert build_context(None, []) == {"epic_summary": "", "existing": [], "known_keys": []}


def test_the_epic_is_shown_to_the_agent_and_citable():
    tree = [
        _ticket("PAY-1", issue_type="Epic", summary="Retry payments", description="Retry failed payments three times."),
        _ticket("PAY-2", parent_key="PAY-1", summary="Backoff", status="In Progress", description="1s, 5s, 30s"),
        _ticket("PAY-3", parent_key="PAY-2", issue_type="Sub-task", summary="Write the schedule"),
    ]
    text, shown = render_epic_context("PAY-1", tree)
    assert "Retry failed payments three times." in text
    assert "PAY-2 — Backoff" in text and "Status: In Progress" in text
    assert "Write the schedule" in text  # a sub-task is listed under its story
    # What was shown is what a citation of it is checked against.
    assert set(shown) == {"PAY-1", "PAY-2", "PAY-3"}
    assert "Status: In Progress" in shown["PAY-2"]
    assert render_epic_context("PAY-1", []) == ("", {})


# -- reading the epic from the source -----------------------------------------------------------------


class _FakeSource:
    name = "jira"

    def __init__(self, tree=None, error=None, normalize_error=None):
        self.tree, self.error, self.normalize_error = tree, error, normalize_error

    def normalize_key(self, raw):
        if self.normalize_error:
            raise ValueError(self.normalize_error)
        return raw.strip()

    def get_ticket_tree(self, key, max_tickets=None):
        if self.error:
            raise self.error
        return self.tree


def _patch_source(monkeypatch, source, *, configured=True):
    monkeypatch.setattr(po_sessions, "ticket_source_configured", lambda project_id: configured)
    monkeypatch.setattr(po_sessions, "get_ticket_source", lambda project_id: source)


def test_inspect_epic_reads_the_tree_and_picks_the_mode(monkeypatch):
    _patch_source(monkeypatch, _FakeSource([EPIC, _ticket("PAY-2", parent_key="PAY-1")]))
    key, mode, tree, source = po_sessions.inspect_epic(1, " PAY-1 ")
    assert (key, mode, source) == ("PAY-1", "gap", "jira") and len(tree) == 2


def test_inspect_epic_refuses_what_it_cannot_work_on(monkeypatch):
    _patch_source(monkeypatch, _FakeSource([_ticket("PAY-9", issue_type="Story")]))
    with pytest.raises(PoModeError, match="not an Epic"):
        po_sessions.inspect_epic(1, "PAY-9")

    _patch_source(monkeypatch, _FakeSource(), configured=False)
    with pytest.raises(PoSourceUnavailable, match="not configured"):
        po_sessions.inspect_epic(1, "PAY-1")

    _patch_source(monkeypatch, _FakeSource(error=RuntimeError("404")))
    with pytest.raises(PoSourceUnavailable, match="Could not read PAY-1"):
        po_sessions.inspect_epic(1, "PAY-1")

    _patch_source(monkeypatch, _FakeSource(normalize_error="not a ticket"))
    with pytest.raises(PoModeError, match="not a ticket"):
        po_sessions.inspect_epic(1, "???")

    _patch_source(monkeypatch, _FakeSource([_ticket("OTHER-1", issue_type="Epic")]))
    with pytest.raises(PoSourceUnavailable, match="was not found"):
        po_sessions.inspect_epic(1, "PAY-1")


# -- the draft ---------------------------------------------------------------------------------------------


def _story(ref="s1", **overrides):
    story = {
        "ref": ref, "summary": f"Do thing {ref}", "description": f"Why {ref} exists.",
        "acceptance_criteria": [f"{ref} behaves"], "issue_type": "Story", "kind": "dev", "labels": [],
        "depends_on": [], "subtasks": [], "evidence": [], "rationale": "", "size": "M",
    }
    story.update(overrides)
    return story


def _stored_draft(mode="split", **overrides):
    draft = {
        "mode": mode,
        "epic": {
            "summary": "Retry payments", "description": "Retry failed payments.", "acceptance_criteria": ["Retries happen"],
            "existing_key": None if mode == "brainstorm" else "PAY-1", "suggested_description": None,
        },
        "stories": [_story("s1"), _story("s2", depends_on=["s1"], subtasks=[{"summary": "Sub", "description": "d"}])],
        "existing": [], "gaps": [], "assumptions": [], "out_of_scope": [],
    }
    draft.update(overrides)
    return draft


def _session(mode="split", draft=None, **overrides):
    session = {
        "id": 1, "project_id": 1, "epic_key": None if mode == "brainstorm" else "PAY-1", "mode": mode, "draft": draft,
        "draft_version": 3, "questions": [], "context": {"epic_summary": "Retry payments", "existing": [], "known_keys": []},
    }
    session.update(overrides)
    return session


def test_the_agents_reply_and_questions_are_not_part_of_the_stored_draft():
    stored = draft_for_storage({**_stored_draft(), "reply": "hi", "questions": [{"id": "q"}]})
    assert "reply" not in stored and "questions" not in stored and stored["stories"]


def test_a_stored_draft_is_acceptable_to_save():
    assert validate_draft_edit(_stored_draft(), _session()) == []


def test_a_saved_draft_may_carry_the_servers_verification_marks():
    draft = _stored_draft()
    draft["stories"][0]["evidence"] = [{"source": "code", "ref": "a.py", "quote": "x", "verified": True}]
    assert validate_draft_edit(draft, _session()) == []


def test_a_saved_draft_must_match_the_schema():
    session = _session()
    assert validate_draft_edit("nope", session) == ["The draft must be a JSON object."]

    bad_enum = _stored_draft()
    bad_enum["stories"][0]["size"] = "XL"
    assert any("size" in e and "one of" in e for e in validate_draft_edit(bad_enum, session))

    extra = _stored_draft()
    extra["stories"][0]["colour"] = "red"
    assert any("colour" in e and "not an accepted argument" in e for e in validate_draft_edit(extra, session))

    missing = _stored_draft()
    del missing["stories"][0]["summary"]
    assert any("summary is required" in e for e in validate_draft_edit(missing, session))

    wrong_mode = _stored_draft("gap")
    assert any("mode" in e for e in validate_draft_edit(wrong_mode, session))


@pytest.mark.usefixtures("elstin_db")  # the story cap is a setting
def test_a_draft_with_a_structural_problem_is_still_saved_and_the_problem_shown():
    draft = _stored_draft()
    draft["stories"][0]["acceptance_criteria"] = []
    session = _session(draft=draft)
    assert validate_draft_edit(draft, session) == []
    problems = structure_problems(session)
    assert any(p["kind"] == "no_criteria" and p["blocking"] for p in problems)
    assert structure_problems(_session(draft=None)) == []


def test_what_a_person_changed_is_named_by_path_and_reordering_is_not_an_edit():
    old = _stored_draft()
    new = copy.deepcopy(old)
    new["epic"]["summary"] = "Retry payments, carefully"
    new["stories"][1]["description"] = "Rewritten by a person"
    new["stories"].append(_story("s3"))
    new["gaps"] = ["A gap"]
    assert diff_paths(old, new) == ["epic.summary", "stories.s2.description", "stories.s3", "gaps"]

    reordered = copy.deepcopy(old)
    reordered["stories"].reverse()
    assert diff_paths(old, reordered) == []

    removed = copy.deepcopy(old)
    removed["stories"].pop(0)
    assert diff_paths(old, removed) == ["stories.s1"]


def test_a_verification_mark_is_not_an_edit():
    old = _stored_draft()
    old["stories"][0]["evidence"] = [{"source": "code", "ref": "a.py", "quote": "x", "verified": True}]
    new = copy.deepcopy(old)
    new["stories"][0]["evidence"][0].pop("verified")
    assert diff_paths(old, new) == []
    new["stories"][0]["evidence"][0]["quote"] = "y"
    assert diff_paths(old, new) == ["stories.s1.evidence"]


def test_what_publishing_would_create():
    draft = _stored_draft("brainstorm", stories=[
        _story("s1"), _story("s2", depends_on=["s1"], subtasks=[{"summary": "a", "description": ""}, {"summary": "b", "description": ""}]),
        _story("s3", depends_on=["s1", "s2", "PAY-9"]),
    ])
    assert publish_counts(_session("brainstorm", draft)) == {"epics": 1, "stories": 3, "subtasks": 2, "links": 4}
    assert publish_counts(_session("split", draft))["epics"] == 0
    assert publish_counts(_session("split", None)) == {"epics": 0, "stories": 0, "subtasks": 0, "links": 0}


def test_what_is_left_to_publish_leaves_out_what_is_already_in_jira():
    draft = _stored_draft("brainstorm", stories=[
        _story("s1", subtasks=[{"summary": "a", "description": ""}]),
        _story("s2", depends_on=["s1"], subtasks=[{"summary": "b", "description": ""}, {"summary": "c", "description": ""}]),
    ])
    session = _session("brainstorm", draft, publish_result={
        "created": {"epic": "PAY-1", "s1": "PAY-2", "s1.1": "PAY-3", "s2.1": "PAY-4"}, "links": [],
    })
    assert publish_counts(session) == {"epics": 0, "stories": 1, "subtasks": 1, "links": 1}
    # One story on its own: just it, and the link to what it depends on.
    assert publish_counts(session, ["s2"]) == {"epics": 0, "stories": 1, "subtasks": 1, "links": 1}
    assert publish_counts(session, ["s1"]) == {"epics": 0, "stories": 0, "subtasks": 0, "links": 0}
    # Once the link exists it is not counted again.
    session["publish_result"]["links"] = ["PAY-2>PAY-5"]
    session["publish_result"]["created"]["s2"] = "PAY-5"
    assert publish_counts(session)["links"] == 0
    # No epic yet: any story brings it along.
    assert publish_counts(_session("brainstorm", draft), ["s2"])["epics"] == 1


def test_an_edit_to_a_story_already_in_jira_is_refused_but_the_rest_of_the_draft_is_not():
    session = _session("brainstorm", _stored_draft(), publish_result={"created": {"epic": "PAY-1", "s1": "PAY-2", "s1.1": "PAY-3"}})
    refused = published_edit_conflicts(session, ["stories.s1.description", "stories.s2.summary", "epic.summary"])
    assert len(refused) == 1 and "s1 is already in JIRA as PAY-2" in refused[0] and "the epic is already in JIRA as PAY-1" in refused[0]
    assert published_edit_conflicts(session, ["stories.s2.summary", "gaps", "assumptions"]) == []
    assert published_edit_conflicts(session, ["stories.s1"])  # removing it is an edit of it too
    # A split's epic is the existing one, which was never made here.
    split = _session("split", _stored_draft("split"), publish_result={"created": {"s1": "PAY-2"}})
    assert published_edit_conflicts(split, ["epic.description"]) == []


def test_a_revision_may_not_rewrite_what_is_already_in_jira():
    old = _stored_draft("brainstorm", stories=[_story("s1", summary="Original s1"), _story("s2"), _story("s3")])
    session = _session("brainstorm", old, publish_result={"created": {"epic": "PAY-1", "s1": "PAY-2"}})
    revised = _stored_draft("brainstorm", stories=[_story("s2", summary="Better s2"), _story("s4")])
    revised["epic"]["summary"] = "Rewritten epic"

    kept = keep_published(revised, old, session)

    assert [(s["ref"], s["summary"]) for s in kept["stories"]] == [("s1", "Original s1"), ("s2", "Better s2"), ("s4", "Do thing s4")]
    assert kept["epic"]["summary"] == "Retry payments"
    assert revised["stories"][0]["ref"] == "s2"  # the agent's own draft is not mutated
    # Nothing published: the revision stands as it is.
    assert keep_published(revised, old, _session("brainstorm", old)) is revised


def test_a_ticket_description_ends_with_the_acceptance_criteria_in_the_form_jira_reads_back():
    text = story_markdown(_story("s1", description="Why.", acceptance_criteria=["It works", " ", "It is fast"]))
    assert text == "Why.\n\n## Acceptance Criteria\n- It works\n- It is fast"
    assert JiraService.parse_acceptance_criteria(text) == ["It works", "It is fast"]
    assert story_markdown(_story("s1", description="Only this.", acceptance_criteria=[])) == "Only this."


def test_the_draft_exports_as_markdown_with_everything_a_person_would_want():
    draft = _stored_draft("gap", stories=[
        _story("s1", labels=["payments"], depends_on=["PAY-2"], rationale="Because.",
               evidence=[{"source": "code", "ref": "src/a.py:3", "quote": "def a():", "verified": False}],
               subtasks=[{"summary": "Write it", "description": ""}]),
    ], existing=[{
        "key": "PAY-2", "assessment": "partial", "suggested_edit": "Update the AC to say three attempts.",
        "evidence": [{"source": "jira", "ref": "PAY-2", "quote": "Status: To Do", "verified": True}],
    }], gaps=["Nothing covers notifications"], assumptions=["Stripe only"], out_of_scope=["Refunds"])
    session = _session("gap", draft, questions=[{"id": "q1", "question": "Which provider?", "options": [{"label": "Stripe", "description": "the default"}]}])
    markdown = draft_to_markdown(session)

    assert markdown.startswith("# Retry payments\n")
    assert "for PAY-1" in markdown and "version 3" in markdown
    assert "## Epic PAY-1" in markdown
    assert "- **PAY-2** — partial" in markdown and "Suggested edit: Update the AC to say three attempts." in markdown
    assert "## Proposed stories" in markdown and "### s1: Do thing s1" in markdown
    assert "depends on PAY-2" in markdown and "labels: payments" in markdown
    assert "## Acceptance Criteria\n- s1 behaves" in markdown
    assert "- Write it" in markdown and "_Why: Because._" in markdown
    assert "(could not be verified)" in markdown
    assert "## Gaps" in markdown and "- Nothing covers notifications" in markdown
    assert "## Assumptions" in markdown and "## Out of scope" in markdown
    assert "## Open questions" in markdown and "- Which provider?" in markdown and "  - Stripe — the default" in markdown


def test_a_brainstorm_exports_its_epic_as_new():
    markdown = draft_to_markdown(_session("brainstorm", _stored_draft("brainstorm")))
    assert "## Epic\n" in markdown and "## Stories" in markdown and "for PAY-1" not in markdown
