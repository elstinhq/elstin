"""Markdown to Atlassian Document Format, for the descriptions elstin writes into
JIRA.

JIRA Cloud's REST API v3 takes a description as ADF — a JSON document — and
nothing else; sending markdown puts the characters `##` and `-` on the page
literally. The PO publisher (elstin/services/backlog_publisher.py) writes
descriptions as markdown because that is what a person edits, so this is the
step between.

Only what a ticket description uses: headings, paragraphs (a single newline
inside one is a line break, as people type them), bullet and numbered lists with
nesting by indentation, fenced code blocks, and inline bold, italic, code and
links. Anything else stays as plain text rather than being guessed at.

The one contract that matters: **a `## Acceptance Criteria` heading followed by
bullets must come back out as those criteria**. elstin reads a ticket's criteria
with `JiraService.parse_acceptance_criteria` over the ticket's text, so a
description it writes has to survive the trip — there is a test that sends a
description through here and back (tests/test_adf.py).
"""
from __future__ import annotations

import re
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*```\s*([\w+-]*)\s*$")
# Inline, longest-first alternatives: `code`, **bold**, [text](url), *italic* / _italic_.
_INLINE_RE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|\[(?P<label>[^\]]+)\]\((?P<href>[^)\s]+)\)"
    r"|(?<![\w*])\*(?P<em1>[^*\s][^*]*?)\*(?![\w*])"
    r"|(?<![\w])_(?P<em2>[^_\s][^_]*?)_(?![\w])"
)


def _text(value: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def _inline(text: str) -> list[dict[str, Any]]:
    """Inline markdown as ADF text nodes. A mark never nests another here: bold
    text containing a link is the link's text, bold — which is as much as a
    ticket description needs, and keeps the output valid."""
    nodes: list[dict[str, Any]] = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            nodes.append(_text(text[position:match.start()]))
        if match.group("code") is not None:
            nodes.append(_text(match.group("code"), [{"type": "code"}]))
        elif match.group("bold") is not None:
            nodes.append(_text(match.group("bold"), [{"type": "strong"}]))
        elif match.group("label") is not None:
            nodes.append(_text(match.group("label"), [{"type": "link", "attrs": {"href": match.group("href")}}]))
        else:
            nodes.append(_text(match.group("em1") or match.group("em2") or "", [{"type": "em"}]))
        position = match.end()
    if position < len(text):
        nodes.append(_text(text[position:]))
    return [node for node in nodes if node["text"]]


def _paragraph(lines: list[str]) -> dict[str, Any] | None:
    content: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if index:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line.strip()))
    return {"type": "paragraph", "content": content} if content else None


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _list_item_match(line: str) -> tuple[str, int, str] | None:
    """(kind, indent, text) for a list line, else None."""
    bullet = _BULLET_RE.match(line.replace("\t", "    "))
    if bullet:
        return "bulletList", len(bullet.group(1)), bullet.group(2)
    ordered = _ORDERED_RE.match(line.replace("\t", "    "))
    if ordered:
        return "orderedList", len(ordered.group(1)), ordered.group(2)
    return None


def _build_list(lines: list[str], start: int) -> tuple[dict[str, Any], int]:
    """The list that begins at `lines[start]`, with its nested lists, and the index
    after it. Nesting is by indentation: a deeper line belongs to the item above."""
    first = _list_item_match(lines[start])
    assert first is not None
    kind, base_indent, _ = first
    items: list[dict[str, Any]] = []
    index = start
    while index < len(lines):
        match = _list_item_match(lines[index])
        if match is None or match[1] < base_indent:
            break
        item_kind, _indent, text = match
        if item_kind != kind:
            break  # a different kind of list at the same level starts a new list
        content: list[dict[str, Any]] = []
        paragraph = _paragraph([text])
        if paragraph:
            content.append(paragraph)
        index += 1
        while index < len(lines):
            nested = _list_item_match(lines[index])
            if nested is None or nested[1] <= base_indent:
                break
            child, index = _build_list(lines, index)
            content.append(child)
        items.append({"type": "listItem", "content": content or [{"type": "paragraph", "content": []}]})
    return {"type": kind, "content": items}, index


def markdown_to_adf(markdown: str) -> dict[str, Any]:
    """`markdown` as an ADF document (see the module docstring for what is
    understood). An empty input is a document with one empty paragraph, which is
    what JIRA itself stores for a blank description."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            paragraph = _paragraph(pending)
            if paragraph:
                blocks.append(paragraph)
            pending.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            index += 1
            code: list[str] = []
            while index < len(lines) and not _FENCE_RE.match(lines[index]):
                code.append(lines[index])
                index += 1
            index += 1  # the closing fence (or the end)
            block: dict[str, Any] = {"type": "codeBlock", "content": [_text("\n".join(code))] if "".join(code) else []}
            if fence.group(1):
                block["attrs"] = {"language": fence.group(1)}
            blocks.append(block)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            content = _inline(heading.group(2))
            blocks.append({"type": "heading", "attrs": {"level": len(heading.group(1))}, "content": content})
            index += 1
            continue
        if _list_item_match(line):
            flush()
            block, index = _build_list(lines, index)
            blocks.append(block)
            continue
        if not line.strip():
            flush()
        else:
            pending.append(line)
        index += 1
    flush()
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}
