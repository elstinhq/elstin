"""PO sessions, the parts that are not storage, not the agent and not the daemon:
choosing a session's mode, what a draft is checked against, what a person's
edit changed, and the draft as text a person can copy out.

**The mode is picked by code, not guessed by the model** (`select_mode`), before
any model call — from the one fact that decides it, what the epic already has:

| input | mode |
|---|---|
| no epic, only a brief | brainstorm |
| an epic with no top-level stories | split |
| an epic with stories | gap |

"Top-level stories" is elstin's own rule for what gets dispatched
(elstin/services/epic_sync.py's `_resolve_work_units`), so a PO session and the
board agree about what an epic's stories are.

**Bug mode is the exception: a person asks for it** (the start page's "Bug"),
since nothing about the epic says whether a brief reports a fault. Its draft is
one Bug ticket (`{"bug": {...}}`, product_owner.py's `BUG_DRAFT_SCHEMA`), not an
epic and stories, and everything below that reads a draft — the edit schema,
what changed, what publishing makes, the markdown — has a bug branch.

**A draft is checked against more than itself.** Duplicates, the epic never
being proposed as new, every existing story being assessed — those need to know
the epic. That is captured into the session's `context` when it starts and on
every turn (`build_context`), so a person's save and a publish can re-run the
structural checks (elstin/agents/roles/po_gate.py's `check_structure`) without a
round trip to JIRA.

**The draft stored is the draft a person edits**: the agent's per-turn `reply`
and `questions` live in the conversation and on the session, not in it
(`draft_for_storage`). `EDIT_SCHEMA` is what a saved edit must still match.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from elstin.agents.roles.po_gate import (
    DraftContext, check_draft_integrity, check_structure, kind_warnings,
)
from elstin.agents.roles.product_owner import BUG_DRAFT_SCHEMA, PO_DRAFT_SCHEMA
from elstin.agents.tool_arguments import argument_errors
from elstin.config.store import get_int_config, resolve_bool_config
from elstin.services.epic_sync import _is_epic_issue_type, _resolve_work_units
from elstin.services.ticket_source import Ticket, get_ticket_source, project_source_name, ticket_source_configured
from elstin.state.manager_po import PO_AWAITING_INPUT, PO_MODE_BRAINSTORM, PO_MODE_BUG, PO_MODE_GAP, PO_MODE_SPLIT, PO_READY
from elstin.tools.po_tools import render_ticket

DEFAULT_MAX_STORIES = 15
_MAX_EPIC_TEXT_CHARS = 8_000
_MAX_STORY_TEXT_CHARS = 2_500
_JIRA_KEY_PREFIX_RE = re.compile(r"^([A-Z][A-Z0-9]+)-\d+$")


class PoModeError(ValueError):
    """The epic cannot be worked on as asked. The message is for the person."""


class PoSourceUnavailable(PoModeError):
    """The ticket source could not be reached or read, as opposed to the request
    being wrong."""


def select_mode(epic_key: str | None, tree: list[Ticket]) -> str:
    """The session's mode (see the module docstring). `tree` is the epic and
    every descendant, root first, as `TicketSource.get_ticket_tree` returns it.
    A lone story that has no children counts as having none: it is split."""
    if not epic_key:
        return PO_MODE_BRAINSTORM
    root = next((ticket for ticket in tree if ticket.key == epic_key), None)
    if root is None:
        raise PoModeError(f"{epic_key} is not in the ticket tree that was read for it.")
    top_level, _subtasks = _resolve_work_units(root, [ticket for ticket in tree if ticket.key != epic_key])
    return PO_MODE_GAP if any(ticket.key != epic_key for ticket in top_level) else PO_MODE_SPLIT


def inspect_epic(project_id: int, raw_key: str) -> tuple[str, str, list[Ticket], str]:
    """(epic key, mode, the ticket tree, the source's name) for an epic a person
    typed, read from the project's ticket source — what starting a session and
    the dialog's "this will be a …" note both ask. Raises PoModeError (the
    request is wrong) or PoSourceUnavailable (the source is not).

    On JIRA only an Epic qualifies: a session proposes stories *under* it, and
    JIRA will not put a story under a story."""
    if not ticket_source_configured(project_id):
        raise PoSourceUnavailable(
            "This project's ticket source is not configured, so the epic cannot be read. Set the connection at "
            "/settings or in the project's settings."
        )
    try:
        source = get_ticket_source(project_id)
        key = source.normalize_key(raw_key)
    except ValueError as exc:
        raise PoModeError(str(exc)) from exc
    try:
        tree = source.get_ticket_tree(key)
    except Exception as exc:  # noqa: BLE001 — a 404, a timeout, a permissions error: all "could not read it"
        raise PoSourceUnavailable(f"Could not read {key} from {source.name}: {exc}") from exc
    root = next((ticket for ticket in tree if ticket.key == key), None)
    if root is None:
        raise PoSourceUnavailable(f"{key} was not found in {source.name}.")
    if source.name == "jira" and not _is_epic_issue_type(root.issue_type):
        raise PoModeError(
            f"{key} is a {root.issue_type}, not an Epic. A PO session works on an epic (it proposes the stories under "
            "it), or on no epic at all to draft a new one."
        )
    return key, select_mode(key, tree), tree, source.name


def top_level_stories(epic_key: str, tree: list[Ticket]) -> list[Ticket]:
    """The epic's own stories — what gap mode assesses and what a proposed
    story must not repeat."""
    root = next((ticket for ticket in tree if ticket.key == epic_key), None)
    if root is None:
        return []
    top_level, _subtasks = _resolve_work_units(root, [ticket for ticket in tree if ticket.key != epic_key])
    return [ticket for ticket in top_level if ticket.key != epic_key]


def jira_project_key_of(epic_key: str | None) -> str | None:
    """`MCS2` from `MCS2-857`."""
    match = _JIRA_KEY_PREFIX_RE.match(epic_key or "")
    return match.group(1) if match else None


def resolve_jira_project_key(explicit: str | None, epic_key: str | None, ticket_filter: dict | None) -> str | None:
    """Where a session's tickets are created: what the person typed, else the
    epic's own project, else the project's saved ticket filter."""
    typed = (explicit or "").strip().upper()
    if typed:
        return typed
    return jira_project_key_of(epic_key) or ((ticket_filter or {}).get("jira_project_key") or "").strip().upper() or None


def build_context(epic_key: str | None, tree: list[Ticket], research_keys: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """What a draft is checked against that is not in the draft, as the JSON
    stored on the session."""
    root = next((ticket for ticket in tree if ticket.key == epic_key), None) if epic_key else None
    return {
        "epic_summary": root.summary if root else "",
        "existing": [[ticket.key, ticket.summary] for ticket in top_level_stories(epic_key, tree)] if epic_key else [],
        "known_keys": sorted({ticket.key for ticket in tree} | set(research_keys)),
    }


def draft_context(session: dict[str, Any]) -> DraftContext:
    """The checks' view of a stored session."""
    context = session.get("context") or {}
    return DraftContext(
        mode=session["mode"],
        epic_key=session.get("epic_key"),
        epic_summary=context.get("epic_summary") or "",
        existing=tuple((str(key), str(summary)) for key, summary in (context.get("existing") or [])),
        known_keys=frozenset(context.get("known_keys") or []),
        max_stories=max(1, get_int_config("po_max_stories", DEFAULT_MAX_STORIES)),
    )


def draft_for_storage(draft: dict[str, Any]) -> dict[str, Any]:
    """The draft a person edits: the agent's `reply` and `questions` are the
    conversation's, and sit beside it."""
    return {key: value for key, value in draft.items() if key not in ("reply", "questions")}


def _edit_schema(draft_schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(draft_schema)
    for key in ("reply", "questions"):
        schema["properties"].pop(key, None)
        schema["required"].remove(key)
    # The checks that need a run's reads mark each evidence item `verified`;
    # a saved edit sends that back, so the schema must allow it.
    props = schema["properties"]
    evidence_holders = (
        [props["bug"]["properties"]] if "bug" in props
        else [props["stories"]["items"]["properties"], props["existing"]["items"]["properties"]]
    )
    for holder in evidence_holders:
        holder["evidence"]["items"]["properties"]["verified"] = {"type": "boolean"}
    return schema


EDIT_SCHEMA: dict[str, Any] = _edit_schema(PO_DRAFT_SCHEMA)
BUG_EDIT_SCHEMA: dict[str, Any] = _edit_schema(BUG_DRAFT_SCHEMA)


def validate_draft_edit(draft: Any, session: dict[str, Any]) -> list[str]:
    """Why a person's saved draft is not acceptable, as messages — empty when
    it is. Only the *shape* is refused: it must match the schema and be a draft
    object. A draft with a structural problem (a story with no criteria, a
    dependency on nothing) is still saved — a person mid-edit has to be able to
    keep their work — and `structure_problems` shows the problem and publish
    refuses on it."""
    if not isinstance(draft, dict):
        return ["The draft must be a JSON object."]
    errors = argument_errors(draft, BUG_EDIT_SCHEMA if session["mode"] == PO_MODE_BUG else EDIT_SCHEMA)
    if errors:
        return errors[:10]
    integrity = check_draft_integrity({**draft, "questions": []}, mode=session["mode"])
    if integrity:
        return [d["reason"] for d in integrity][:10]
    return []


def structure_problems(session: dict[str, Any], draft: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The structural defects of the session's draft (or `draft`), as the
    editor shows them and publish refuses on: blocking ones and warnings."""
    draft = draft if draft is not None else session.get("draft")
    if not isinstance(draft, dict):
        return []
    ctx = draft_context(session)
    return [*check_structure({**draft, "questions": []}, ctx), *kind_warnings(draft)]


def diff_paths(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Dotted paths of what a person changed between two drafts
    ("stories.s2.description", "epic.summary", "gaps") — what the next revise
    turn is told not to undo. Stories and existing assessments are matched by
    their ref / key, so reordering is not an edit."""
    old = old or {}
    paths: list[str] = []

    def compare(prefix: str, before: Any, after: Any) -> None:
        if before != after:
            paths.append(prefix)

    # `verified` marks are server-written, not an edit.
    strip = lambda items: [{k: v for k, v in e.items() if k != "verified"} for e in items or [] if isinstance(e, dict)]

    if isinstance(new.get("bug"), dict) or isinstance(old.get("bug"), dict):
        before_bug, after_bug = old.get("bug") or {}, new.get("bug") or {}
        for field in sorted(set(before_bug) | set(after_bug)):
            if field == "evidence":
                compare("bug.evidence", strip(before_bug.get(field)), strip(after_bug.get(field)))
            else:
                compare(f"bug.{field}", before_bug.get(field), after_bug.get(field))

    for field in sorted(set((old.get("epic") or {}).keys()) | set((new.get("epic") or {}).keys())):
        compare(f"epic.{field}", (old.get("epic") or {}).get(field), (new.get("epic") or {}).get(field))

    for group, id_field in (("stories", "ref"), ("existing", "key")):
        before = {row.get(id_field): row for row in old.get(group) or [] if isinstance(row, dict)}
        after = {row.get(id_field): row for row in new.get(group) or [] if isinstance(row, dict)}
        for ident in [i for i in before if i not in after]:
            paths.append(f"{group}.{ident}")
        for ident, row in after.items():
            if ident not in before:
                paths.append(f"{group}.{ident}")
                continue
            for field in sorted(set(before[ident]) | set(row)):
                if field == "evidence":
                    compare(f"{group}.{ident}.evidence", strip(before[ident].get(field)), strip(row.get(field)))
                else:
                    compare(f"{group}.{ident}.{field}", before[ident].get(field), row.get(field))

    for group in ("gaps", "assumptions", "out_of_scope"):
        compare(group, old.get(group), new.get(group))
    return paths


# -- what the brief shows the agent ------------------------------------------------------


def render_epic_context(epic_key: str, tree: list[Ticket]) -> tuple[str, dict[str, str]]:
    """(the epic and its stories as text for the prompt, {key: the text shown
    for it}) — the second is what the agent may cite without opening the ticket
    again (elstin/tools/po_tools.py's `PoResearch`)."""
    root = next((ticket for ticket in tree if ticket.key == epic_key), None)
    if root is None:
        return "", {}
    shown: dict[str, str] = {}
    stories = top_level_stories(epic_key, tree)
    parts: list[str] = []
    epic_text = render_ticket(root, stories)
    parts.append(epic_text[: _MAX_EPIC_TEXT_CHARS])
    shown[root.key] = epic_text
    for story in stories:
        subtasks = [t for t in tree if t.parent_key == story.key and t.key != story.key]
        rendered = render_ticket(story, subtasks)
        shown[story.key] = rendered
        for sub in subtasks:
            shown[sub.key] = f"{sub.key} {sub.summary}"
        parts.append("---\n" + rendered[:_MAX_STORY_TEXT_CHARS])
    return "\n\n".join(parts), shown


# -- the draft as text ---------------------------------------------------------------------


def acceptance_section(criteria: list[str]) -> str:
    """The `## Acceptance Criteria` block a ticket's description ends with, in
    the form `JiraService.parse_acceptance_criteria` reads back."""
    items = [c.strip() for c in criteria if isinstance(c, str) and c.strip()]
    return ("## Acceptance Criteria\n" + "\n".join(f"- {item}" for item in items)) if items else ""


def story_markdown(story: dict[str, Any]) -> str:
    """A story's description with its acceptance criteria — what its ticket will say."""
    parts = [str(story.get("description") or "").strip(), acceptance_section(story.get("acceptance_criteria") or [])]
    return "\n\n".join(part for part in parts if part)


_CONFIDENCE_WORDS = {"high": "high", "medium": "medium — depends on data or configuration", "low": "low — where, not yet why"}


def bug_markdown(bug: dict[str, Any]) -> str:
    """A bug's description — what its ticket will say, and what elstin's planner
    reads when it plans the fix. The acceptance criteria come last, in the form
    `JiraService.parse_acceptance_criteria` reads back."""

    def section(heading: str, body: str) -> str:
        body = body.strip()
        return f"## {heading}\n{body}" if body else ""

    def bullets(items: Any, numbered: bool = False) -> str:
        clean = [str(item).strip() for item in items or [] if isinstance(item, str) and item.strip()]
        return "\n".join(f"{index}. {item}" if numbered else f"- {item}" for index, item in enumerate(clean, start=1))

    cause_heading = "Root cause"
    if bug.get("confidence") in _CONFIDENCE_WORDS:
        cause_heading += f" (confidence: {_CONFIDENCE_WORDS[bug['confidence']]})"
    cause = str(bug.get("root_cause") or "")
    if bug.get("cause_in") in ("data", "configuration", "unknown"):
        cause = f"_The cause lies in {bug['cause_in']}{'' if bug['cause_in'] != 'unknown' else ' — it could not be placed'}._\n\n{cause}"
    evidence = "\n".join(f"- {_evidence_line(ev)}" for ev in bug.get("evidence") or [])
    parts = [
        section("Observed", str(bug.get("observed") or "")),
        section("Expected", str(bug.get("expected") or "")),
        section("Steps to reproduce", bullets(bug.get("steps_to_reproduce"), numbered=True)),
        section(cause_heading, cause),
        section("Where the fix lies", str(bug.get("fix_location") or "")),
        section("To confirm outside the code", bullets(bug.get("to_confirm"))),
        section("Evidence", evidence),
        acceptance_section(bug.get("acceptance_criteria") or []),
    ]
    return "\n\n".join(part for part in parts if part)


def epic_markdown(epic: dict[str, Any]) -> str:
    parts = [str(epic.get("description") or "").strip(), acceptance_section(epic.get("acceptance_criteria") or [])]
    return "\n\n".join(part for part in parts if part)


def created_tickets(session: dict[str, Any]) -> dict[str, str]:
    """{ref -> JIRA key} of what a publish has made so far (the checkpoint): `epic`,
    story refs (`s1`) and sub-tasks (`s1.2`)."""
    return dict((session.get("publish_result") or {}).get("created") or {})


def publish_counts(session: dict[str, Any], refs: list[str] | None = None) -> dict[str, int]:
    """What publishing would create from here, for the preview: an epic only in a
    brainstorm that has not made it yet, each proposed story not yet in JIRA, their
    sub-tasks, and one link per dependency. `refs` narrows it to those stories —
    the epic stays in, since a story is made under it."""
    draft = session.get("draft") or {}
    result = session.get("publish_result") or {}
    created = created_tickets(session)
    if session["mode"] == PO_MODE_BUG:
        return {"epics": 0, "stories": 0, "subtasks": 0, "links": 0, "bugs": 0 if "bug" in created or not isinstance(draft.get("bug"), dict) else 1}
    made_links = set(result.get("links") or [])
    stories = [s for s in draft.get("stories") or [] if isinstance(s, dict) and (refs is None or s.get("ref") in refs)]
    subtasks = links = 0
    for story in stories:
        ref = story.get("ref")
        # The publisher numbers a story's sub-tasks from 1 over the whole list, blanks included.
        subtasks += sum(1 for index in range(1, len(story.get("subtasks") or []) + 1) if f"{ref}.{index}" not in created)
        for dependency in (d for d in (story.get("depends_on") or []) if d):
            blocked = created.get(ref)
            if blocked and f"{created.get(dependency, dependency)}>{blocked}" in made_links:
                continue
            links += 1
    return {
        "epics": 1 if session["mode"] == PO_MODE_BRAINSTORM and "epic" not in created else 0,
        "stories": sum(1 for story in stories if story.get("ref") not in created),
        "subtasks": subtasks,
        "links": links,
        "bugs": 0,
    }


_STORY_AT_RE = re.compile(r"^stories\[(\d+)\]")


def published_edit_conflicts(session: dict[str, Any], paths: list[str]) -> list[str]:
    """Why a person's edit (`diff_paths`) cannot be saved: it changes a ticket that
    is already in JIRA — a story published on its own, or the epic of a brainstorm.
    The ticket would not follow the draft, so the draft would stop being what was
    made. Empty when the edit touches only what is not in JIRA yet."""
    created = created_tickets(session)
    found: dict[str, str] = {}
    for path in paths:
        head, _, rest = path.partition(".")
        ref = rest.split(".", 1)[0]
        if head == "stories" and ref in created and "." not in ref:
            found[ref] = f"{ref} is already in JIRA as {created[ref]}"
        elif head == "epic" and session["mode"] == PO_MODE_BRAINSTORM and "epic" in created:
            found["epic"] = f"the epic is already in JIRA as {created['epic']}"
        elif head == "bug" and "bug" in created:
            found["bug"] = f"the bug is already in JIRA as {created['bug']}"
    if not found:
        return []
    return [f"{'; '.join(found.values())} — it can no longer be changed here. Change the ticket in JIRA."]


def keep_published(new: dict[str, Any], old: dict[str, Any] | None, session: dict[str, Any]) -> dict[str, Any]:
    """The agent's revised draft with what is already in JIRA put back as it was:
    published stories (and a brainstorm's epic) are final, so a revision may add
    around them but not rewrite, merge or drop them. The agent is told this in its
    prompt; this is what makes it true."""
    created = created_tickets(session)
    if not old or not created:
        return new
    kept = copy.deepcopy(new)
    if session["mode"] == PO_MODE_BUG:
        if "bug" in created and isinstance(old.get("bug"), dict):
            kept["bug"] = copy.deepcopy(old["bug"])
        return kept
    if session["mode"] == PO_MODE_BRAINSTORM and "epic" in created and isinstance(old.get("epic"), dict):
        kept["epic"] = copy.deepcopy(old["epic"])
    published = {
        s["ref"]: (index, s) for index, s in enumerate(old.get("stories") or [])
        if isinstance(s, dict) and s.get("ref") in created
    }
    stories = [s for s in kept.get("stories") or [] if isinstance(s, dict) and s.get("ref") not in published]
    for ref, (index, story) in sorted(published.items(), key=lambda item: item[1][0]):
        stories.insert(min(index, len(stories)), copy.deepcopy(story))
    kept["stories"] = stories
    return kept


# The PO agent's two JIRA writes, each behind a setting of its own (elstin/config/store.py).
# Neither is `jira_write_enabled` — the framework's transitions and QA comments — and
# neither depends on it. The publisher passes these to the ticket source, which checks
# them again.
PO_CREATE_GUARD = "po_jira_create_enabled"
PO_COMMENT_GUARD = "po_jira_comment_enabled"

# The one reason a person can clear on the spot, by typing the project key at publish.
# The page recognises it by this text (ui-src/.../po/draftOps.ts's NEEDS_PROJECT_KEY_REASON).
NEEDS_PROJECT_KEY_REASON = "Say which JIRA project the tickets go in."


def _publish_facts(session: dict[str, Any], jira_project_key: str | None = None) -> dict[str, Any]:
    """What publishing depends on that is not the draft: the project's ticket
    source and its two switches. Read once, so a page of per-story answers does not
    ask the settings once per story."""
    project_id = session["project_id"]
    source = project_source_name(project_id)
    configured = ticket_source_configured(project_id)
    return {
        "source": source,
        "configured": configured,
        "create_enabled": source == "jira" and configured and resolve_bool_config(PO_CREATE_GUARD, project_id),
        "comments_allowed": source == "jira" and configured and resolve_bool_config(PO_COMMENT_GUARD, project_id),
        "key": (jira_project_key or session.get("jira_project_key") or "").strip().upper() or None,
    }


def _scoped_refusal(session: dict[str, Any], refs: list[str]) -> str | None:
    """Why these stories, on their own, cannot be published — or None. A story is
    made once, and after the stories it depends on: JIRA links a blocker that
    exists, and a draft that is half in JIRA must still read sensibly."""
    stories = {s["ref"]: s for s in (session.get("draft") or {}).get("stories") or [] if isinstance(s, dict) and s.get("ref")}
    created = created_tickets(session)
    for ref in refs:
        if ref not in stories:
            return f"There is no story {ref} in this draft."
        if ref in created:
            return f"{ref} is already in JIRA as {created[ref]}."
    for ref in refs:
        for dependency in stories[ref].get("depends_on") or []:
            if dependency in stories and dependency not in created and dependency not in refs:
                return f"{ref} depends on {dependency}, which is not in JIRA yet — publish {dependency} first."
    return None


def _publish_reason(
    session: dict[str, Any], facts: dict[str, Any], defects: list[dict[str, Any]], refs: list[str] | None = None,
) -> str | None:
    """The first reason the session — or, with `refs`, just those stories — cannot
    be published now, in the words a person needs; None when it can."""
    if facts["source"] != "jira":
        return (
            "Publishing is only built for JIRA projects — this one's tickets come from GitHub Issues. "
            "Copy the draft as markdown and create the issues from it."
        )
    if not facts["configured"]:
        return "JIRA is not configured for this project. Set the connection at /settings or in the project's settings."
    if not facts["create_enabled"]:
        return (
            f"Publishing is switched off for this project ({PO_CREATE_GUARD}, under PO agent in settings). "
            "Turn it on to publish, or copy the draft as markdown."
        )
    if session["status"] == PO_AWAITING_INPUT:
        return "The agent has open questions. Answer them, or dismiss them to publish on its best guesses."
    if session["status"] != PO_READY:
        return f"The session is {session['status']}; a draft can be published once it is ready."
    if not isinstance(session.get("draft"), dict):
        return "There is no draft yet."
    if not facts["key"]:
        return NEEDS_PROJECT_KEY_REASON
    blocking = [d for d in defects if d.get("blocking", True)]
    stories = session["draft"].get("stories") or []
    created = created_tickets(session)
    # Nothing can be done about a story that is already a ticket: its problems hold nothing back.
    in_jira = {i for i, s in enumerate(stories) if isinstance(s, dict) and s.get("ref") in created}
    blocking = [d for d in blocking if not (m := _STORY_AT_RE.match(d.get("where") or "")) or int(m.group(1)) not in in_jira]
    if refs is not None:
        refusal = _scoped_refusal(session, refs)
        if refusal:
            return refusal
        # A story's own problems hold it back; another story's do not.
        mine = {i for i, s in enumerate(stories) if isinstance(s, dict) and s.get("ref") in refs}
        blocking = [d for d in blocking if not (m := _STORY_AT_RE.match(d.get("where") or "")) or int(m.group(1)) in mine]
    if blocking:
        what = "in the draft first" if refs is None else f"in {', '.join(refs)} first"
        return f"Fix {len(blocking)} problem{'s' if len(blocking) != 1 else ''} {what} — {blocking[0]['reason']}"
    counts = publish_counts(session, refs)
    if not (counts["epics"] or counts["stories"] or counts["bugs"]):
        if created:
            return "Everything in this draft is already in JIRA."
        return "The draft proposes nothing to create. Existing tickets are never rewritten; post a suggestion as a comment instead."
    return None


def publish_state(
    session: dict[str, Any], *, jira_project_key: str | None = None, refs: list[str] | None = None,
    defects: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Whether the session can be published, and if not why — the first reason
    that applies, in the words a person needs. The publish route refuses (409)
    with the same `reason`; the page shows it beside a disabled button. With
    `refs`, whether just those stories can be (see `story_publish_states`).

    `comments_allowed` is the separate question of posting an existing ticket's
    suggested edit as a comment, which has its own gate (`po_jira_comment_enabled`)."""
    facts = _publish_facts(session, jira_project_key)
    reason = _publish_reason(session, facts, structure_problems(session) if defects is None else defects, refs)
    return {"allowed": reason is None, "reason": reason, "jira_project_key": facts["key"], "comments_allowed": facts["comments_allowed"]}


def story_publish_states(session: dict[str, Any], defects: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Per proposed story: the JIRA `key` it was made as (None until it is), whether
    it can be published on its own now (`allowed`, else `reason`), and what doing so
    would create (`counts`). What the story's own Publish button reads."""
    draft = session.get("draft")
    if not isinstance(draft, dict):
        return {}
    facts = _publish_facts(session)
    defects = structure_problems(session) if defects is None else defects
    created = created_tickets(session)
    states: dict[str, dict[str, Any]] = {}
    for story in draft.get("stories") or []:
        ref = story.get("ref") if isinstance(story, dict) else None
        if not isinstance(ref, str) or not ref or ref in states:
            continue
        key = created.get(ref)
        reason = None if key else _publish_reason(session, facts, defects, [ref])
        states[ref] = {"key": key, "allowed": key is None and reason is None, "reason": reason, "counts": publish_counts(session, [ref])}
    return states


def draft_to_markdown(session: dict[str, Any]) -> str:
    """The draft as a markdown document — "Copy as markdown", which works
    whether or not elstin may write to JIRA."""
    draft = session.get("draft") or {}
    if session["mode"] == PO_MODE_BUG:
        return _bug_draft_markdown(session, draft)
    epic = draft.get("epic") or {}
    mode = session["mode"]
    lines: list[str] = []
    title = epic.get("summary") or "Backlog draft"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        f"_PO draft ({mode})" + (f" for {session.get('epic_key')}" if session.get("epic_key") else "")
        + f" · version {session.get('draft_version')}_"
    )
    if mode == PO_MODE_BRAINSTORM:
        lines += ["", "## Epic", "", epic_markdown(epic)]
    else:
        lines += ["", f"## Epic {session.get('epic_key')}", "", epic_markdown(epic)]
        if epic.get("suggested_description"):
            lines += ["", "### Suggested epic description", "", str(epic["suggested_description"]).strip()]

    existing = [row for row in draft.get("existing") or [] if isinstance(row, dict)]
    if existing:
        lines += ["", "## Existing stories", ""]
        for row in existing:
            lines.append(f"- **{row.get('key')}** — {row.get('assessment')}")
            for ev in row.get("evidence") or []:
                lines.append(f"  - {_evidence_line(ev)}")
            if row.get("suggested_edit"):
                lines.append(f"  - Suggested edit: {str(row['suggested_edit']).strip()}")

    for heading, key in (("Gaps", "gaps"),):
        items = [i for i in draft.get(key) or [] if isinstance(i, str) and i.strip()]
        if items:
            lines += ["", f"## {heading}", ""] + [f"- {item}" for item in items]

    stories = [s for s in draft.get("stories") or [] if isinstance(s, dict)]
    if stories:
        lines += ["", "## Proposed stories" if existing else "## Stories"]
    for story in stories:
        lines += [
            "",
            f"### {story.get('ref')}: {story.get('summary')}",
            "",
            f"_{story.get('issue_type')} · {story.get('kind')} · size {story.get('size')}"
            + (f" · depends on {', '.join(story.get('depends_on') or [])}" if story.get("depends_on") else "")
            + (f" · labels: {', '.join(story.get('labels') or [])}" if story.get("labels") else "")
            + "_",
            "",
            story_markdown(story),
        ]
        if story.get("subtasks"):
            lines += ["", "**Sub-tasks**"] + [f"- {sub.get('summary')}" for sub in story["subtasks"] if isinstance(sub, dict)]
        if story.get("rationale"):
            lines += ["", f"_Why: {str(story['rationale']).strip()}_"]
        if story.get("evidence"):
            lines += ["", "**Evidence**"] + [f"- {_evidence_line(ev)}" for ev in story["evidence"]]

    for heading, key in (("Assumptions", "assumptions"), ("Out of scope", "out_of_scope")):
        items = [i for i in draft.get(key) or [] if isinstance(i, str) and i.strip()]
        if items:
            lines += ["", f"## {heading}", ""] + [f"- {item}" for item in items]

    lines += _open_questions_lines(session)
    return "\n".join(lines).rstrip() + "\n"


def _bug_draft_markdown(session: dict[str, Any], draft: dict[str, Any]) -> str:
    bug = draft.get("bug") or {}
    lines = [
        f"# {bug.get('summary') or 'Bug draft'}",
        "",
        "_Bug draft" + (f" under {session.get('epic_key')}" if session.get("epic_key") else "")
        + f" · version {session.get('draft_version')}"
        + (f" · labels: {', '.join(bug.get('labels') or [])}" if bug.get("labels") else "") + "_",
        "",
        bug_markdown(bug),
    ]
    for heading, key in (("Assumptions", "assumptions"), ("Out of scope", "out_of_scope")):
        items = [i for i in draft.get(key) or [] if isinstance(i, str) and i.strip()]
        if items:
            lines += ["", f"## {heading}", ""] + [f"- {item}" for item in items]
    lines += _open_questions_lines(session)
    return "\n".join(lines).rstrip() + "\n"


def _open_questions_lines(session: dict[str, Any]) -> list[str]:
    questions = [q for q in session.get("questions") or [] if isinstance(q, dict)]
    if not questions:
        return []
    lines = ["", "## Open questions", ""]
    for question in questions:
        lines.append(f"- {question.get('question')}")
        for option in question.get("options") or []:
            lines.append(f"  - {option.get('label')}" + (f" — {option['description']}" if option.get("description") else ""))
    return lines


def _evidence_line(evidence: Any) -> str:
    if not isinstance(evidence, dict):
        return str(evidence)
    mark = "" if evidence.get("verified") is not False else " (could not be verified)"
    return f"{evidence.get('source')} `{evidence.get('ref')}`: \"{str(evidence.get('quote') or '').strip()}\"{mark}"
