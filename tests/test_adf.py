"""Markdown to ADF (elstin/services/adf.py) — pure — and the contract that matters:
a description elstin writes comes back out as the same acceptance criteria."""
from __future__ import annotations

import pytest

from elstin.services.adf import markdown_to_adf
from elstin.services.jira_service import JiraService
from elstin.services.po_sessions import acceptance_section, epic_markdown, story_markdown


def _doc(markdown: str) -> list[dict]:
    doc = markdown_to_adf(markdown)
    assert doc["type"] == "doc" and doc["version"] == 1
    return doc["content"]


def _text_of(node: dict) -> str:
    if node.get("type") == "text":
        return node["text"]
    return "".join(_text_of(child) for child in node.get("content", []))


def test_an_empty_description_is_a_document_with_one_empty_paragraph():
    assert markdown_to_adf("")["content"] == [{"type": "paragraph", "content": []}]
    assert markdown_to_adf("   \n\n ")["content"] == [{"type": "paragraph", "content": []}]


def test_headings_keep_their_level():
    blocks = _doc("# One\n\n### Three ###\n\nbody")
    assert [(b["type"], b.get("attrs", {}).get("level")) for b in blocks] == [("heading", 1), ("heading", 3), ("paragraph", None)]
    assert _text_of(blocks[1]) == "Three"


def test_paragraphs_split_on_blank_lines_and_a_single_newline_is_a_line_break():
    blocks = _doc("first line\nsecond line\n\nanother paragraph")
    assert len(blocks) == 2
    assert [n["type"] for n in blocks[0]["content"]] == ["text", "hardBreak", "text"]


def test_bullet_and_numbered_lists():
    blocks = _doc("- one\n- two\n\n1. first\n2. second")
    assert [b["type"] for b in blocks] == ["bulletList", "orderedList"]
    assert [_text_of(item) for item in blocks[0]["content"]] == ["one", "two"]
    assert [_text_of(item) for item in blocks[1]["content"]] == ["first", "second"]
    assert all(item["type"] == "listItem" and item["content"][0]["type"] == "paragraph" for item in blocks[0]["content"])


def test_nested_lists_hang_on_the_item_above():
    blocks = _doc("- parent\n  - child a\n  - child b\n- sibling")
    assert len(blocks) == 1 and len(blocks[0]["content"]) == 2
    parent = blocks[0]["content"][0]
    assert [c["type"] for c in parent["content"]] == ["paragraph", "bulletList"]
    assert [_text_of(i) for i in parent["content"][1]["content"]] == ["child a", "child b"]
    assert _text_of(blocks[0]["content"][1]) == "sibling"


def test_a_list_of_another_kind_at_the_same_level_is_a_new_list():
    assert [b["type"] for b in _doc("- a\n1. b")] == ["bulletList", "orderedList"]


def test_a_fenced_code_block_is_kept_verbatim():
    blocks = _doc("before\n\n```python\nif x:\n    # not a heading\n    pass\n```\n\nafter")
    assert [b["type"] for b in blocks] == ["paragraph", "codeBlock", "paragraph"]
    assert blocks[1]["attrs"] == {"language": "python"}
    assert blocks[1]["content"][0]["text"] == "if x:\n    # not a heading\n    pass"
    assert "attrs" not in _doc("```\nplain\n```")[0]


def test_inline_marks():
    content = _doc("a **bold** and *italic* and `code` and [a link](https://x.test/p) and _under_")[0]["content"]
    marks = {n["text"]: [m["type"] for m in n.get("marks", [])] for n in content}
    assert marks["bold"] == ["strong"] and marks["italic"] == ["em"] and marks["code"] == ["code"]
    assert marks["a link"] == ["link"] and marks["under"] == ["em"]
    link = next(n for n in content if n["text"] == "a link")
    assert link["marks"][0]["attrs"] == {"href": "https://x.test/p"}
    assert all(n["text"] for n in content)  # ADF forbids empty text nodes


def test_a_snake_case_word_is_not_italic():
    content = _doc("call do_the_thing now")[0]["content"]
    assert len(content) == 1 and "marks" not in content[0]


@pytest.mark.parametrize("markdown", ["- ", "#", "**", "```", "[](", "* *", "1.", "- \n-"])
def test_odd_input_never_raises_and_never_makes_empty_text(markdown):
    def check(node):
        if node.get("type") == "text":
            assert node["text"]
        for child in node.get("content", []):
            check(child)

    check(markdown_to_adf(markdown))


# -- the contract -----------------------------------------------------------------------------------------


def _round_trip(markdown: str) -> list[str]:
    """Written as ADF, read back the way elstin reads a ticket (over REST v3)."""
    return JiraService.parse_acceptance_criteria(JiraService.parse_adf_to_text(markdown_to_adf(markdown)))


def test_a_description_with_criteria_reads_back_as_those_criteria():
    story = {
        "description": "Why this exists.\n\nIt matters **a lot**.", "acceptance_criteria": ["An email is queued", "No email on success"],
    }
    assert _round_trip(story_markdown(story)) == ["An email is queued", "No email on success"]


def test_an_epics_criteria_round_trip_too():
    epic = {"description": "# Problem\nRetries.\n\n## Scope\n- a\n- b\n\n## Out of scope\n- c", "acceptance_criteria": ["Retries happen"]}
    assert _round_trip(epic_markdown(epic)) == ["Retries happen"]


def test_criteria_with_inline_marks_read_back_as_their_text():
    assert _round_trip("## Acceptance Criteria\n- Uses `retry()` from [utils](https://x.test)\n- It is **fast**") == [
        "Uses retry() from utils", "It is fast",
    ]


def test_the_heading_is_found_whichever_way_jira_returns_it():
    # REST v2 returns the same heading as wiki markup.
    wiki = "Some text\n\nh2. Acceptance Criteria\n* first\n* second\n\nh2. Notes\n* not a criterion"
    assert JiraService.parse_acceptance_criteria(wiki) == ["first", "second"]
    assert JiraService.parse_acceptance_criteria("## AC\n- one") == ["one"]
    assert JiraService.parse_acceptance_criteria("Acceptance criteria without a heading marker\n- x") == []


def test_no_criteria_is_no_section():
    assert acceptance_section([]) == "" and acceptance_section([" ", ""]) == ""
    assert _round_trip(story_markdown({"description": "Just prose.", "acceptance_criteria": []})) == []


def test_text_headings_keep_their_hashes_when_an_adf_description_is_read():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance Criteria"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "plain"}]},
    ]}
    assert JiraService.parse_adf_to_text(adf) == "## Acceptance Criteria\nplain"
