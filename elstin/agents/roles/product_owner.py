"""The PO agent's prompts, draft schema and prompt assembly — the pure half of
the role, kept apart from elstin/agents/roles/po_loop.py's orchestration for the
reason planner.py is kept apart from planner_loop.py.

What the PO agent is for. elstin starts from a ticket a person already wrote, and
how good that ticket is decides much of what happens after: the kickoff
experiment (MCS2-857, run 2) showed that a richer epic description cut reviewer
rejections from 8 to 4 and stopped invented content. This role writes that
backlog — an epic split into stories and tasks, with acceptance criteria,
dependencies and cited evidence — from the project's code, its Confluence
pages and its JIRA tickets, as a *draft* a person edits and then publishes
(elstin/services/backlog_publisher.py). It writes nothing itself.

**The mode is chosen by code, not by the model** (`select_mode` in
elstin/services/po_sessions.py), and this module only words it:

- brainstorm — a brief and no epic: propose the epic and its stories;
- split — an epic with no stories: break it down; suggest, never rewrite;
- gap — an epic with stories: assess each, find what nothing covers, propose
  only what is new, never rewrite an existing ticket.

**Whose acceptance criteria these become.** Each story the draft produces is
planned and built later by elstin's own agents, which read its acceptance
criteria as the bar a reviewer holds the diff to. That reviewer can see the
repository and the build's result and nothing else, so a criterion that needs a
production database, a release or somebody's sign-off fails a story after three
rounds. The prompt says so, because an author who has not been told writes
exactly those.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from elstin.services.lessons import format_lessons_block

# A nullable-string leaf: OpenAI's strict json_schema mode requires every
# property at every level to be in `required`, and "the model may skip this" is
# spelled `["string", "null"]` rather than omission (planner_loop's schema).
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_STRINGS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["code", "jira", "confluence"]},
        "ref": {
            "type": "string",
            "description": "code: a repo-relative path, optionally :line (@label/path in a multi-repo project); "
            "jira: the ticket key; confluence: the page id",
        },
        "quote": {"type": "string", "description": "A short excerpt copied verbatim from what you read"},
    },
    "required": ["source", "ref", "quote"],
    "additionalProperties": False,
}

_STORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "A short label for this draft only: s1, s2, …"},
        "summary": {"type": "string"},
        "description": {"type": "string"},
        "acceptance_criteria": _STRINGS,
        "issue_type": {"type": "string", "enum": ["Story", "Task"]},
        "kind": {"type": "string", "enum": ["dev", "qa"]},
        "labels": _STRINGS,
        "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Refs of stories in this draft, or existing ticket keys"},
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"summary": {"type": "string"}, "description": {"type": "string"}},
                "required": ["summary", "description"],
                "additionalProperties": False,
            },
        },
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
        "rationale": {"type": "string", "description": "Why this story exists and is shaped this way"},
        "size": {"type": "string", "enum": ["S", "M", "L"]},
    },
    "required": [
        "ref", "summary", "description", "acceptance_criteria", "issue_type", "kind", "labels", "depends_on",
        "subtasks", "evidence", "rationale", "size",
    ],
    "additionalProperties": False,
}

_EXISTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "assessment": {"type": "string", "enum": ["done", "partial", "not_started", "obsolete", "unclear"]},
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
        "suggested_edit": {**_NULLABLE_STRING, "description": "What should change in this ticket, for a person to post as a comment; null if nothing"},
    },
    "required": ["key", "assessment", "evidence", "suggested_edit"],
    "additionalProperties": False,
}

# The planner's clarification-question shape (planner_loop._PLAN_JSON_SCHEMA), so
# the UI's question panel needs one rendering for both.
_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "question": {"type": "string"},
        "context": _NULLABLE_STRING,
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "label": {"type": "string"}, "description": _NULLABLE_STRING},
                "required": ["id", "label", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["id", "question", "context", "options"],
    "additionalProperties": False,
}

PO_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["brainstorm", "split", "gap"]},
        "epic": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "description": {"type": "string"},
                "acceptance_criteria": _STRINGS,
                "existing_key": _NULLABLE_STRING,
                "suggested_description": _NULLABLE_STRING,
            },
            "required": ["summary", "description", "acceptance_criteria", "existing_key", "suggested_description"],
            "additionalProperties": False,
        },
        "stories": {"type": "array", "items": _STORY_SCHEMA},
        "existing": {"type": "array", "items": _EXISTING_SCHEMA},
        "gaps": _STRINGS,
        "assumptions": _STRINGS,
        "out_of_scope": _STRINGS,
        "questions": {"type": "array", "items": _QUESTION_SCHEMA},
        "reply": {"type": "string"},
    },
    "required": ["mode", "epic", "stories", "existing", "gaps", "assumptions", "out_of_scope", "questions", "reply"],
    "additionalProperties": False,
}

PO_SYSTEM_CORE = """You are a senior product owner drafting a backlog for an engineering team. A person will review and edit your draft, may talk it over with you, and then publishes it to JIRA, where an autonomous coding platform (elstin) picks the stories up: each top-level story is planned, built and reviewed by its own agent run in an isolated checkout, independent stories run in parallel, and a story waits only for the stories it depends on.

Return ONE JSON object matching the schema you were given, nothing else, with these fields:
- mode — exactly the mode named in the user message.
- epic — {summary, description, acceptance_criteria, existing_key, suggested_description}.
- stories — the proposed stories and tasks (see "Writing a story").
- existing — an assessment of each existing story (gap mode only; [] otherwise).
- gaps — things the epic, its acceptance criteria or a Confluence page promise that no story covers (strings; gap mode, else []).
- assumptions — what you took as given because nothing said otherwise.
- out_of_scope — what you deliberately left out, so nobody expects it.
- questions — see "Questions".
- reply — a short message to the person: what you drafted, the decisions you made that they should look at, and anything you could not settle. Plain prose, no markup, never a restatement of the whole draft.

RESEARCH FIRST. You have read-only tools: the repositories (find_files_tool, grep_tool, read_file_tool, outline_file_tool), and — when the project has them — JIRA (jira_search, jira_get_issue), Confluence (confluence_search, confluence_get_page) and elstin's own board (board_status). A backlog written without looking at the code proposes work that exists and misses work that does not. Look for: what the code already does; what the epic, the Confluence pages and earlier JIRA tickets say about it; who else has already tried this. The user message carries what has already been gathered; check anything you rely on heavily yourself.

EVIDENCE. Every claim a person might want to check carries an evidence entry: {source, ref, quote}.
- code: ref is a repo-relative path (`@label/path` in a multi-repo project), optionally `:line`.
- jira: ref is a ticket key you opened with jira_get_issue (or that the user message shows).
- confluence: ref is a page id you opened with confluence_get_page (or that the user message shows).
- quote is a short excerpt COPIED VERBATIM from what you read — a sentence or a line, never a paraphrase and never from memory. A gate compares each quote with the real file, ticket or page, and sends the draft back if one is not there, or if you cite something you never opened.
- Cite fewer things you are sure of rather than many you are not. A story needs evidence when the code or a document shaped it; a plainly new story needs none.

WRITING A STORY.
- summary: one line in the imperative, specific enough to tell the story from its neighbours.
- description: why the story exists, what is in it and what is not, the constraints the code or the documents impose, and where in the code the work most likely lies. Not an implementation plan — elstin's planner writes that from the code. Plain markdown, no acceptance criteria in it (they have their own field).
- acceptance_criteria: at least one, each a statement a reviewer can check by reading the diff and the build's and the tests' result. The agents that build and review the story can read and edit the repositories and run the project's build and tests; they cannot reach a production database, data extracts, other branches, JIRA, Confluence or the network, and cannot publish, release or sign anything off. So NEVER write a criterion that needs one of those ("verified against production", "the team signs off", "released in 2.3"). Say what the change delivers instead (the script is committed and takes its input as a parameter; the endpoint returns the field), and put the human step under out_of_scope or in the description as something left to a person.
- A story is small enough for ONE agent run: a handful of files in one area, one behaviour a reviewer can hold in their head. If you want to write "and" in the summary, it is probably two stories. Size S is a few files, M is one subsystem, L must be justified in `rationale` or split — an L story is one elstin may not finish.
- Tests belong in the story that makes the change, never in a story of their own. A QA story (kind "qa") is for validating a whole feature end to end across what the other stories built; elstin runs it after every dev story automatically, so propose at most one, and only when an end-to-end check is genuinely worth a story. Everything else is kind "dev".
- issue_type: "Story" for user-visible or behavioural work, "Task" for technical work (a migration, a refactor, tooling). Use `subtasks` for the file- or step-level breakdown inside one story ({summary, description}); sub-tasks are delivered with their story, never on their own.
- depends_on: only where one story truly cannot start before another is merged — a shared type, an API another story calls, a migration it needs. Each dependency costs parallelism, so prefer slicing the work so most stories need none; when two stories share a contract, put the contract first and say so. Refer to stories of this draft by ref and to existing tickets by key. Never a loop.
- labels: only ones the project already uses, and only when they matter.
- size and rationale: honest. `rationale` says why the story exists and is shaped as it is, in a sentence or two.
- Never propose a story an existing ticket already covers (the gate rejects a repeated summary); say under `existing` or `gaps` instead.

QUESTIONS. Ask only when a decision changes what gets built, you looked and the code and documents do not settle it, and a wrong guess would waste a story. At most three; each with a short id, the question, the evidence it turns on in `context`, and two to four options (most idiomatic first). Otherwise decide, and record the decision under assumptions. A draft goes out with its questions — the person may answer them later, so write the draft on your best choice for each.

DO NOT INVENT. No file, API, ticket or page you did not see. If something you needed is not in the repositories, JIRA or Confluence, say so in assumptions or a question rather than filling the gap.

OUTPUT HYGIENE. Every value is plain text or the declared type: arrays are arrays, never a JSON list inside a string, and never tool-call markup (<parameter>, <invoke>) inside a value.
"""

_MODE_BRAINSTORM = """MODE: brainstorm — there is no epic yet. Draft one from the person's brief.
- epic.summary and epic.description: the description is markdown with the sections "Problem", "Scope" and "Out of scope", written so an engineer who has not seen the brief understands what is wanted and why. epic.acceptance_criteria: what must be true when the whole epic is done (at least one, checkable under the rules above).
- epic.existing_key is null; epic.suggested_description is null; `existing` is [].
- stories: the split of the epic into stories and tasks, with dependencies. At least one."""

_MODE_SPLIT = """MODE: split — the epic already exists and has no stories yet. Break it down.
- epic.existing_key is the epic's key (given below). Copy the epic's own summary and description into epic.summary / epic.description and its acceptance criteria into epic.acceptance_criteria as they stand — you never rewrite the epic. If you believe a sharper description would help the split, put it in epic.suggested_description (otherwise null); a person decides whether it is used.
- `existing` is []. stories: the split of the epic into stories and tasks, with dependencies. At least one."""

_MODE_GAP = """MODE: gap — the epic already has stories. Find out where it stands and what is missing. You never rewrite an existing ticket.
- epic.existing_key is the epic's key. Copy the epic's summary, description and acceptance criteria as they stand.
- existing: ONE entry for EVERY existing story listed below. assessment is one of: done (the work exists — in the code, or merged on elstin's board), partial (some of it), not_started (nothing yet), obsolete (no longer needed given the code or the documents), unclear (you could not tell — say what you would need). Base it on what you can see: the ticket's status in JIRA, elstin's board (board_status — a story can be merged in elstin and still To Do in JIRA, and that disagreement is worth saying), and above all the code. Back each assessment with evidence. suggested_edit: when the ticket's text should change (a criterion that is out of date, a missing constraint), say what, as a sentence or two a person can post as a comment on it; null when nothing. The ticket itself is never edited.
- gaps: what the epic, its acceptance criteria or the Confluence pages promise that no story — existing or proposed — covers.
- stories: ONLY what is new: work no existing story covers. May be empty when nothing is missing. Never repeat or re-propose an existing story, and do not propose the epic."""

_MODE_TEXT = {"brainstorm": _MODE_BRAINSTORM, "split": _MODE_SPLIT, "gap": _MODE_GAP}

_REVISE_RULES = """REVISING. This is a revision of a draft that already exists — the CURRENT DRAFT in the user message — not a new one.
- Start from the current draft and return the WHOLE draft again with the change made. Keep every story ref as it is; add new refs for new stories; do not renumber.
- Change only what the person's new message, answers or the new information ask for. Everything else stays word for word.
- A person may have edited the draft by hand (the user message says so and lists the fields). Their edits win: keep those fields exactly as they now stand, and never put back what they changed or removed — unless their new message directly asks you to change that very thing, in which case say that you did in `reply`.
- `reply` answers the person's message directly and says what you changed. If they only asked a question, answer it and return the draft unchanged."""


def build_po_system(mode: str, *, revising: bool = False) -> str:
    """The system prompt for a turn in `mode`."""
    parts = [PO_SYSTEM_CORE, _MODE_TEXT.get(mode, _MODE_TEXT["brainstorm"])]
    if revising:
        parts.append(_REVISE_RULES)
    return "\n\n".join(parts)


# -- the scoping and explorer prompts (explorer.gather_exploration's `scope_system` /
#    `explorer_system`): the same mechanism the planner uses, worded for a backlog.

PO_SCOPE_SYSTEM = """You are scoping the code investigation for a backlog a product owner is about to write.

You get a brief (and, usually, an epic, what is already known about it, and a map of the repository's source files). Decide which parts of the codebase the product owner must understand before it can split this work into stories — what already exists, how the relevant parts are built, where the seams between them are — and turn each into ONE self-contained question for a read-only agent that will investigate it in parallel with the others.

Rules:
- Each area must be answerable by reading code, independent of the other areas, and worth a dedicated reader. Split by part of the system (the UI layer, the API, the data model, an existing integration), never by step of the work.
- Each question must stand alone. The agent answering it sees only the question, not the brief or the other areas.
- Ask what EXISTS ("how does the current retry logic work, and where?"), not what to build.
- path_hints are optional starting points from the file map, not instructions — leave the list empty when you have none.
- Return FEWER areas than the maximum when the brief is narrow, and an EMPTY list when it names its own files or is obviously small. An unnecessary investigation costs real money and teaches nothing.
"""

PO_EXPLORER_SYSTEM = """You are a recon specialist helping a product owner write a backlog: first in, travels light, reports what is actually there.

You receive ONE self-contained investigation task. Answer it from the repository itself, not from assumption.

- Ground every claim in a file you actually read. Cite locations as `path:line`, and copy short excerpts verbatim — the product owner will quote them, and its quotes are checked against the files.
- Search broadly first, then read only the few files that matter. Never read a whole large file when a range answers the question — your read tool serves at most 250 lines per call, so use grep to find the line and offset/limit to read around it.
- You are read-only. You cannot write, edit or run anything, and you are not drafting the backlog — another agent does that with your report in hand.
- Say what already exists, what is partly there, and what is plainly absent. Name the seams: the interfaces, shared types and entry points that one piece of work would have to agree with another on.
- If the task cannot be answered from the repository, say so plainly and state what is missing. A confident wrong answer is far worse than "this is not in the repo".
- Answer with a compact report: the direct answer first, then the evidence, then anything a backlog must account for. Short headings and bullets. No preamble, no restating the task.
"""


@dataclass
class PoInputs:
    """What the daemon gathered for a turn, as plain text and data. The role
    turns it into the user message (`build_po_user_text`); it does not fetch any
    of it (elstin/daemon/po_runner.py does)."""

    mode: str
    person_brief: str
    epic_key: str | None = None
    # The epic and, per story, its status and text — rendered.
    epic_context: str = ""
    # elstin's board for that epic — rendered.
    board_context: str = ""
    # The Confluence pages the epic links to — rendered, capped.
    linked_pages: str = ""
    # elstin/services/project_guide.py's guide dict (+ accepted lessons).
    guide: dict[str, Any] | None = None
    skeleton: str = ""
    # A revise turn: {draft, version, edited_by_person, person_edits, history,
    # new_messages, answers}. None for the first turn.
    revise: dict[str, Any] | None = None
    # Whether the project's JIRA / Confluence are reachable for this run — said
    # in the prompt, so the agent is not left to discover a tool is missing.
    capabilities: list[str] = field(default_factory=list)


_MAX_HISTORY_CHARS = 12_000


def _format_history(history: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in history:
        who = "Person" if message.get("role") == "person" else "You (PO agent)"
        lines.append(f"{who}: {str(message.get('text') or '').strip()}")
    text = "\n\n".join(lines)
    if len(text) > _MAX_HISTORY_CHARS:
        text = "…[earlier messages left out]\n" + text[-_MAX_HISTORY_CHARS:]
    return text


def _format_answers(answers: list[dict[str, Any]]) -> str:
    lines = []
    for answer in answers:
        lines.append(f"- Q: {answer.get('question', '')}\n  A: {answer.get('answer', '')}")
    return "\n".join(lines)


def build_po_user_text(inputs: PoInputs, exploration: str = "") -> str:
    """The user message for a turn. `exploration` is the code-investigation
    block, which only exists once the role has run the explorers."""
    guide = inputs.guide or {}
    text = f"MODE: {inputs.mode}\n"
    if inputs.epic_key:
        text += f"EPIC: {inputs.epic_key}\n"
    text += "\n"
    if inputs.capabilities:
        text += "Tools available beyond the code: " + ", ".join(inputs.capabilities) + ".\n\n"

    text += f"=== BRIEF FROM A PERSON ===\n{inputs.person_brief.strip()}\n=== END BRIEF ===\n\n"

    if guide.get("found") and guide.get("content"):
        text += (
            "=== PROJECT GUIDE (the repository's own AGENTS.md / CLAUDE.md) — what the code is and how it is laid out ===\n"
            + (
                "Note: elstin generated this guide from the repository's manifests, README and CI files. Where it "
                "disagrees with what you read in the repository, the repository is right.\n\n"
                if guide.get("generated") else ""
            )
            + guide["content"]
            + "\n=== END GUIDE ===\n\n"
        )
    text += format_lessons_block(guide.get("lessons"))

    if inputs.epic_context:
        text += f"=== THE EPIC AND ITS STORIES (from JIRA) ===\n{inputs.epic_context}\n=== END EPIC ===\n\n"
    if inputs.board_context:
        text += f"=== ELSTIN'S BOARD FOR THIS EPIC ===\n{inputs.board_context}\n=== END BOARD ===\n\n"
    if inputs.linked_pages:
        text += (
            "=== CONFLUENCE PAGES THE EPIC LINKS TO (you may cite them) ===\n"
            f"{inputs.linked_pages}\n=== END PAGES ===\n\n"
        )
    if inputs.skeleton:
        text += inputs.skeleton.rstrip() + "\n\n"
    if exploration:
        text += exploration.rstrip() + "\n\n"

    revise = inputs.revise
    if revise:
        text += f"=== CURRENT DRAFT (version {revise.get('version')}) ===\n"
        text += json.dumps(revise.get("draft"), indent=2, ensure_ascii=False) + "\n=== END CURRENT DRAFT ===\n\n"
        if revise.get("edited_by_person"):
            paths = revise.get("person_edits") or []
            text += (
                "A person has edited this draft by hand. Their edits stand — keep them exactly"
                + (f". Fields they changed: {', '.join(paths[:40])}" if paths else "")
                + "\n\n"
            )
        if revise.get("published"):
            made = ", ".join(f"{ref} ({key})" for ref, key in revise["published"].items())
            text += (
                "Part of this draft is already in JIRA as real tickets, and is final: "
                f"{made}. Keep each of these exactly as it is in the current draft — do not change, split, merge, "
                "reorder or drop them (a change you make to one is discarded). You may add stories around them and "
                "make new stories depend on them.\n\n"
            )
        if revise.get("history"):
            text += f"=== THE CONVERSATION SO FAR ===\n{_format_history(revise['history'])}\n=== END CONVERSATION ===\n\n"
        if revise.get("answers"):
            text += (
                "=== THE PERSON ANSWERED YOUR QUESTIONS (decisions: build them in, do not ask again) ===\n"
                f"{_format_answers(revise['answers'])}\n=== END ANSWERS ===\n\n"
            )
        if revise.get("new_messages"):
            text += (
                "=== NEW MESSAGE(S) FROM THE PERSON — this is what to act on ===\n"
                + "\n\n".join(str(m).strip() for m in revise["new_messages"])
                + "\n=== END NEW MESSAGES ===\n\n"
            )
        text += "Revise the draft as asked and return the whole draft as one JSON object.\n"
    else:
        text += (
            "Research with your tools, then return the draft as one JSON object. Before you write the draft, "
            "look at what the code already does about this — a backlog is only as good as that.\n"
        )
    return text
