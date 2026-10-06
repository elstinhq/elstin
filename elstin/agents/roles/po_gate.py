"""Tier 1 for the PO agent's *draft*, as plan_gate.py is for the planner's plan:
deterministic checks, no model, run on every draft before a person sees it —
and, for the half that needs nothing fetched, again on every edit a person
saves and on publish.

Why a backlog draft needs a gate. A draft is a document someone will act on:
the tickets it becomes are what elstin then plans, builds and merges. The ways it
goes wrong are the ones a machine can see — a story with nothing to check it
against, a dependency on a story that does not exist (or a loop of them, which
no order of work can satisfy), the epic proposed again as if new, a story the
epic already has under another number — and one a machine can audit but a
reader cannot: **evidence**. Every claim carries a citation (a file, a ticket, a
Confluence page) and a verbatim quote, and a model that cites what it never
opened writes a quote that is not there. Whether `src/retry.py` contains that
sentence, or whether the agent ever read PAY-14, is answerable, so it is
answered here rather than left for a reviewer to trust.

Two halves, split by what they need:

- `check_structure` — needs only the draft and what the session knows of the
  epic. This is what a person's saved edit and a publish are held to.
- `verify_evidence` — needs the files in the workspace and what the agent
  actually read this run (elstin/tools/po_tools.py's `PoResearch`). Only the
  agent's own turn can be held to it: a person editing a draft has no run to
  have read anything in.

Blocking vs warning follows plan_gate's rule: a defect blocks when acting on it
would go wrong (a loop of dependencies, a story with no acceptance criteria);
it warns when it only casts doubt (elstin would classify a story's kind another
way). Only blockers spend a retry.

Deliberately NOT checked: whether the stories *cover* the brief, or whether the
split is a good one. That is judgement; a gate that guesses at it rejects good
drafts and teaches the agent to pad.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any

from elstin.agents.roles.plan_gate import (
    KIND_MALFORMED_OUTPUT, array_defects, blocking, check_questions, looks_like_placeholder, markup_defects,
)
from elstin.services.story_kind import classify_story_kind
from elstin.tools.path_guard import PathGuardError
from elstin.tools.po_tools import PoResearch
from elstin.tools.workspace import Workspace

KIND_WRONG_MODE = "wrong_mode"
KIND_STUB_DRAFT = "stub_draft"
KIND_STORY_COUNT = "story_count"
KIND_NO_CRITERIA = "no_criteria"
KIND_STUB_FIELD = "stub_field"
KIND_BAD_REF = "bad_ref"
KIND_UNKNOWN_DEPENDENCY = "unknown_dependency"
KIND_DEPENDENCY_CYCLE = "dependency_cycle"
KIND_DEPENDS_ON_QA = "depends_on_qa"
KIND_DUPLICATE_STORY = "duplicate_story"
KIND_EPIC_REPROPOSED = "epic_reproposed"
KIND_EXISTING_COVERAGE = "existing_coverage"
KIND_LARGE_STORY = "large_story"
KIND_EVIDENCE_MISSING = "evidence_missing"
KIND_EVIDENCE_UNREAD = "evidence_unread"
KIND_EVIDENCE_QUOTE = "evidence_quote"
KIND_KIND_MISMATCH = "kind_mismatch"

STORY_ARRAY_FIELDS = ("acceptance_criteria", "labels", "depends_on", "subtasks", "evidence")
DRAFT_ARRAY_FIELDS = ("stories", "existing", "gaps", "assumptions", "out_of_scope", "questions")
EVIDENCE_SOURCES = ("code", "jira", "confluence")
ASSESSMENTS = ("done", "partial", "not_started", "obsolete", "unclear")

_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_CODE_REF_RE = re.compile(r"^(?P<path>.+?)(?::(?P<line>\d+)(?:-(?P<last>\d+))?)?$")
_PAGE_ID_RE = re.compile(r"(?:/pages/|[?&]pageId=)(\d+)|^(\d+)$")
_MAX_READ_BYTES = 2_000_000
_MIN_RATIONALE_CHARS = 15
_MAX_LISTED = 12


@dataclass(frozen=True)
class DraftContext:
    """What a draft is checked against that is not in the draft."""

    mode: str
    epic_key: str | None = None
    epic_summary: str = ""
    # (key, summary) of the epic's top-level stories — what gap mode must
    # assess and what no proposed story may duplicate.
    existing: tuple[tuple[str, str], ...] = ()
    # Every ticket key a story may depend on: the epic's own tree, plus any
    # the agent read in its research.
    known_keys: frozenset[str] = field(default_factory=frozenset)
    max_stories: int = 15


def _defect(kind: str, reason: str, *, where: str | None = None, is_blocking: bool = True) -> dict[str, Any]:
    return {"kind": kind, "reason": reason, "where": where, "blocking": is_blocking, "path": None, "suggestions": []}


def _normalize(text: str) -> str:
    """The comparison key for "is this the same summary": case, punctuation
    and spacing dropped, like elstin/state/manager_lesson.py's lesson match."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _squash(text: str) -> str:
    """Whitespace collapsed to single spaces: a quote that matches but for
    where the line breaks fell is still verbatim."""
    return re.sub(r"\s+", " ", text or "").strip()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _criteria(value: Any) -> list[str]:
    return [item.strip() for item in _list(value) if isinstance(item, str) and item.strip()]


def _stories(draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [story for story in _list(draft.get("stories")) if isinstance(story, dict)]


# -- integrity -------------------------------------------------------------------


def check_draft_integrity(draft: Any, *, mode: str | None = None) -> list[dict[str, Any]]:
    """Whether `draft` is a draft object at all, ahead of whether it is a good
    one, on the answer as it arrived: tool-call markup swallowing the fields
    after it, an array field that is not an array, an epic with no summary. A
    mangled answer fails the checks below for the wrong reason, so when this
    finds anything it stands in for them (plan_gate.check_plan_integrity)."""
    if not isinstance(draft, dict):
        return [_defect(KIND_MALFORMED_OUTPUT, "the answer is not a JSON object")]
    defects = markup_defects(draft, noun="draft")
    defects.extend(array_defects(draft, DRAFT_ARRAY_FIELDS))
    for index, story in enumerate(_list(draft.get("stories"))):
        if not isinstance(story, dict):
            defects.append(_defect(KIND_MALFORMED_OUTPUT, f"stories[{index}] must be an object", where=f"stories[{index}]"))
            continue
        defects.extend(
            {**d, "where": f"stories[{index}]"} for d in array_defects(story, STORY_ARRAY_FIELDS)
        )
    if not isinstance(draft.get("epic"), dict):
        defects.append(_defect(KIND_MALFORMED_OUTPUT, '"epic" must be an object', where="epic"))
    if mode and draft.get("mode") != mode:
        defects.append(_defect(
            KIND_WRONG_MODE, f'"mode" is {draft.get("mode")!r} but this session is in {mode!r} mode', where="mode",
        ))
    return defects


# -- structure -------------------------------------------------------------------


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """One dependency cycle among the keys of `edges` as a path that ends where
    it began (["s1", "s2", "s1"]), or None. A depth-first walk rather than
    epic_sync's reachability pass: that one only breaks a cycle, and the agent
    has to be told which stories form it."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in edges.get(node, []):
            if nxt not in edges:
                continue
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if state.get(nxt) is None:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for key in edges:
        if state.get(key) is None:
            found = visit(key)
            if found:
                return found
    return None


def check_structure(draft: dict[str, Any], ctx: DraftContext) -> list[dict[str, Any]]:
    """Everything about a draft that needs nothing fetched. Assumes the draft
    passed `check_draft_integrity`; tolerates a missing field rather than
    raising, since a person's edit may leave one out."""
    defects: list[dict[str, Any]] = []
    epic = draft.get("epic") if isinstance(draft.get("epic"), dict) else {}
    stories = _stories(draft)
    modes_with_existing = ctx.mode in ("split", "gap")

    # -- the epic ---------------------------------------------------------------
    if not _text(epic.get("summary")) or looks_like_placeholder(epic.get("summary")):
        defects.append(_defect(KIND_STUB_DRAFT, "the epic has no summary", where="epic.summary"))
    if ctx.mode == "brainstorm":
        if not _text(epic.get("description")) or looks_like_placeholder(epic.get("description")):
            defects.append(_defect(KIND_STUB_FIELD, "the new epic has no description (problem, scope, out of scope)", where="epic.description"))
        if not _criteria(epic.get("acceptance_criteria")):
            defects.append(_defect(KIND_NO_CRITERIA, "the new epic has no acceptance criteria", where="epic.acceptance_criteria"))
        if epic.get("existing_key"):
            defects.append(_defect(KIND_EPIC_REPROPOSED, "a brainstorm creates a new epic, so epic.existing_key must be null", where="epic.existing_key"))
    elif modes_with_existing and epic.get("existing_key") != ctx.epic_key:
        defects.append(_defect(
            KIND_EPIC_REPROPOSED,
            f"this session works on the existing epic {ctx.epic_key}: epic.existing_key must be {ctx.epic_key!r}, "
            "never null — the epic is not proposed as new, and it is never rewritten (use suggested_description)",
            where="epic.existing_key",
        ))

    # -- how many ---------------------------------------------------------------
    if len(stories) > ctx.max_stories:
        defects.append(_defect(
            KIND_STORY_COUNT,
            f"{len(stories)} stories proposed, the most allowed is {ctx.max_stories}: merge the small ones or leave the "
            "least important to out_of_scope — each story must be small enough for one agent run, and a draft this "
            "long is not one a person can review",
            where="stories",
        ))
    if not stories and not (ctx.mode == "gap"):
        defects.append(_defect(KIND_STORY_COUNT, "the draft proposes no stories", where="stories"))

    # -- each story ---------------------------------------------------------------
    existing_keys = {key for key, _summary in ctx.existing}
    refs: dict[str, int] = {}
    for index, story in enumerate(stories):
        where = f"stories[{index}]"
        ref = _text(story.get("ref"))
        if not _REF_RE.match(ref):
            defects.append(_defect(KIND_BAD_REF, f"{where} has ref {ref!r}: a short label like s1, then s2 (letters, digits, - and _)", where=f"{where}.ref"))
        elif ref in refs:
            defects.append(_defect(KIND_BAD_REF, f"ref {ref!r} is used by two stories ({where} and stories[{refs[ref]}])", where=f"{where}.ref"))
        elif ref in existing_keys or _JIRA_KEY_RE.match(ref):
            defects.append(_defect(KIND_BAD_REF, f"ref {ref!r} looks like a JIRA key; a proposed story gets a label like s1 — its key is made at publish", where=f"{where}.ref"))
        if ref and ref not in refs:
            refs[ref] = index

        label = f"story {ref or index + 1}"
        if not _text(story.get("summary")) or looks_like_placeholder(story.get("summary")):
            defects.append(_defect(KIND_STUB_FIELD, f"{label} has no summary", where=f"{where}.summary"))
        if not _text(story.get("description")) or looks_like_placeholder(story.get("description")):
            defects.append(_defect(KIND_STUB_FIELD, f"{label} has no description", where=f"{where}.description"))
        if not _criteria(story.get("acceptance_criteria")) or all(
            looks_like_placeholder(item) for item in _criteria(story.get("acceptance_criteria"))
        ):
            defects.append(_defect(
                KIND_NO_CRITERIA,
                f"{label} has no acceptance criteria — each story needs at least one checkable statement a reviewer can "
                "test the finished work against",
                where=f"{where}.acceptance_criteria",
            ))
        if story.get("issue_type") not in ("Story", "Task"):
            defects.append(_defect(KIND_STUB_FIELD, f'{label}: issue_type must be "Story" or "Task", not {story.get("issue_type")!r}', where=f"{where}.issue_type"))
        if story.get("kind") not in ("dev", "qa"):
            defects.append(_defect(KIND_STUB_FIELD, f'{label}: kind must be "dev" or "qa", not {story.get("kind")!r}', where=f"{where}.kind"))
        if story.get("size") == "L" and (
            len(_text(story.get("rationale"))) < _MIN_RATIONALE_CHARS or looks_like_placeholder(story.get("rationale"))
        ):
            defects.append(_defect(
                KIND_LARGE_STORY,
                f"{label} is size L with no rationale: say why it cannot be split (an L story is one elstin may not finish "
                "in a single run), or split it",
                where=f"{where}.rationale",
            ))
        for sub_index, sub in enumerate(_list(story.get("subtasks"))):
            if not isinstance(sub, dict) or not _text(sub.get("summary")):
                defects.append(_defect(KIND_STUB_FIELD, f"{label}: subtasks[{sub_index}] has no summary", where=f"{where}.subtasks[{sub_index}]"))

    # -- dependencies ---------------------------------------------------------------
    kind_of = {ref: stories[index].get("kind") for ref, index in refs.items()}
    graph: dict[str, list[str]] = {}
    for ref, index in refs.items():
        story = stories[index]
        targets: list[str] = []
        for dep in _list(story.get("depends_on")):
            dep = dep.strip() if isinstance(dep, str) else ""
            if not dep:
                continue
            if dep == ref:
                defects.append(_defect(KIND_DEPENDENCY_CYCLE, f"story {ref} depends on itself", where=f"stories[{index}].depends_on"))
            elif dep in refs:
                targets.append(dep)
                if kind_of.get(dep) == "qa" and story.get("kind") != "qa":
                    defects.append(_defect(
                        KIND_DEPENDS_ON_QA,
                        f"story {ref} depends on the QA story {dep}; elstin runs QA after every dev story and ignores a "
                        "dev story's link to one",
                        where=f"stories[{index}].depends_on", is_blocking=False,
                    ))
            elif dep not in ctx.known_keys and dep not in existing_keys:
                known = ", ".join(sorted(refs)) or "none"
                defects.append(_defect(
                    KIND_UNKNOWN_DEPENDENCY,
                    f"story {ref} depends on {dep!r}, which is neither a story in this draft ({known}) nor a ticket "
                    "you read",
                    where=f"stories[{index}].depends_on",
                ))
        graph[ref] = targets
    cycle = _find_cycle(graph)
    if cycle:
        defects.append(_defect(
            KIND_DEPENDENCY_CYCLE,
            f"the stories depend on each other in a loop ({' → '.join(cycle)}); no order of work satisfies it — "
            "remove one of those dependencies",
            where="stories",
        ))

    # -- duplicates -----------------------------------------------------------------
    taken: dict[str, str] = {}
    if modes_with_existing and ctx.epic_summary:
        taken[_normalize(ctx.epic_summary)] = f"the epic {ctx.epic_key}"
    for key, summary in ctx.existing:
        taken.setdefault(_normalize(summary), f"the existing story {key}")
    for index, story in enumerate(stories):
        norm = _normalize(_text(story.get("summary")))
        if not norm:
            continue
        if norm in taken:
            defects.append(_defect(
                KIND_DUPLICATE_STORY,
                f'story {_text(story.get("ref")) or index + 1} ("{_text(story.get("summary"))}") has the same summary as '
                f"{taken[norm]} — do not propose what already exists; if it needs changing, say so under `existing`",
                where=f"stories[{index}].summary",
            ))
        else:
            taken[norm] = f"story {_text(story.get('ref')) or index + 1} of this draft"

    # -- what already exists (gap mode) ------------------------------------------------
    existing_rows = [row for row in _list(draft.get("existing")) if isinstance(row, dict)]
    if ctx.mode == "gap":
        seen: set[str] = set()
        for index, row in enumerate(existing_rows):
            key = _text(row.get("key"))
            where = f"existing[{index}]"
            if key not in existing_keys:
                defects.append(_defect(KIND_EXISTING_COVERAGE, f"existing[{index}] assesses {key!r}, which is not a story of this epic", where=where))
            elif key in seen:
                defects.append(_defect(KIND_EXISTING_COVERAGE, f"{key} is assessed twice", where=where))
            seen.add(key)
            if row.get("assessment") not in ASSESSMENTS:
                defects.append(_defect(KIND_STUB_FIELD, f"{key or where}: assessment must be one of {', '.join(ASSESSMENTS)}", where=f"{where}.assessment"))
        missing = sorted(existing_keys - seen)
        if missing:
            defects.append(_defect(
                KIND_EXISTING_COVERAGE,
                f"every existing story needs an assessment; missing: {', '.join(missing)}",
                where="existing",
            ))
    elif existing_rows:
        defects.append(_defect(KIND_EXISTING_COVERAGE, f"`existing` is for gap mode; this session is in {ctx.mode} mode, so it must be empty", where="existing"))

    # -- questions --------------------------------------------------------------------------
    questions = [q for q in _list(draft.get("questions")) if isinstance(q, dict)]
    defects.extend(check_questions(questions))
    ids = [_text(q.get("id")) for q in questions]
    if len(set(ids)) != len(ids) or "" in ids:
        defects.append(_defect(KIND_STUB_FIELD, "every question needs its own non-empty id", where="questions"))
    return defects


def kind_warnings(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Stories whose `kind` elstin would classify the other way when the ticket
    is synced back (elstin/services/story_kind.py). Advisory: the heuristic
    proposes and a person disposes, so this only says so while the label can
    still be changed. A QA story is labelled `qa` at publish, so it is judged
    as it will be written."""
    warnings: list[dict[str, Any]] = []
    for index, story in enumerate(_stories(draft)):
        stated = story.get("kind")
        if stated not in ("dev", "qa"):
            continue
        labels = [label for label in _list(story.get("labels")) if isinstance(label, str)]
        if stated == "qa" and "qa" not in {label.lower() for label in labels}:
            labels = [*labels, "qa"]
        classified, reason = classify_story_kind(
            summary=_text(story.get("summary")), description=_text(story.get("description")),
            issue_type=_text(story.get("issue_type")), labels=labels,
        )
        if classified != stated:
            ref = _text(story.get("ref")) or str(index + 1)
            warnings.append(_defect(
                KIND_KIND_MISMATCH,
                f'story {ref} is marked {stated} but elstin would classify it {classified} ({reason}) when the ticket '
                f'is synced — add a "{stated}" label, or reword it, so the two agree',
                where=f"stories[{index}].kind", is_blocking=False,
            ))
    return warnings


# -- evidence ----------------------------------------------------------------------------


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(_MAX_READ_BYTES)
    except OSError:
        return None


def _corrected_ref(path: str, cited: str | None, last: str | None, actual: int | None) -> str | None:
    """`path:line` when the quote starts somewhere the cited line (or range) does
    not cover; None to leave the ref as written — including a ref with no line,
    which claims nothing to be wrong."""
    if not cited or actual is None:
        return None
    if int(cited) <= actual <= int(last or cited):
        return None
    return f"{path}:{actual}"


def _locate_quote(content: str, quote: str, cited: int | None) -> int | None:
    """The 1-based line a verbatim `quote` starts on — the occurrence nearest the
    line the agent cited when it appears more than once. `quote` is already
    squashed, so the file is squashed line by line to find where it lands."""
    pieces: list[tuple[int, str]] = [(n, _squash(line)) for n, line in enumerate(content.splitlines(), start=1)]
    pieces = [(n, text) for n, text in pieces if text]
    starts: list[int] = []
    offset = 0
    for _n, text in pieces:
        starts.append(offset)
        offset += len(text) + 1
    joined = " ".join(text for _n, text in pieces)
    found: list[int] = []
    at = joined.find(quote)
    while at != -1:
        index = max(i for i, start in enumerate(starts) if start <= at)
        found.append(pieces[index][0])
        at = joined.find(quote, at + 1)
    if not found:
        return None
    return min(found, key=lambda n: abs(n - cited)) if cited else found[0]


def _check_one_evidence(item: Any, research: PoResearch, workspace: Workspace) -> tuple[bool, str | None, str, str | None]:
    """(verified, why not, which kind of defect, a corrected ref). The corrected
    ref is set only when a code quote is real but the line cited is not where it
    is — an agent counts lines badly, and a person follows the citation."""
    if not isinstance(item, dict):
        return False, "is not an object", KIND_EVIDENCE_MISSING, None
    source, ref, quote = item.get("source"), _text(item.get("ref")), _squash(_text(item.get("quote")))
    if source not in EVIDENCE_SOURCES:
        return False, f"has source {source!r}, which must be one of {', '.join(EVIDENCE_SOURCES)}", KIND_EVIDENCE_MISSING, None
    if not ref:
        return False, "has no ref", KIND_EVIDENCE_MISSING, None
    if not quote:
        return False, "has no quote — quote the sentence or line that supports the claim, verbatim", KIND_EVIDENCE_QUOTE, None

    if source == "code":
        match = _CODE_REF_RE.match(ref)
        path, line = match.group("path"), match.group("line")
        try:
            absolute = workspace.resolve_read(path)
        except (PathGuardError, KeyError, ValueError) as exc:
            return False, f"names a path that cannot be read ({exc})", KIND_EVIDENCE_MISSING, None
        if not os.path.isfile(absolute):
            return False, f"names {path}, which is not a file in the workspace", KIND_EVIDENCE_MISSING, None
        content = _read_text(absolute)
        if content is None:
            return False, f"names {path}, which could not be read", KIND_EVIDENCE_MISSING, None
        if line and int(line) > len(content.splitlines()):
            return False, f"cites line {line}, but {path} has only {len(content.splitlines())} line(s)", KIND_EVIDENCE_MISSING, None
        if quote not in _squash(content):
            return False, f"quotes text that is not in {path}", KIND_EVIDENCE_QUOTE, None
        return True, None, "", _corrected_ref(path, match.group("line"), match.group("last"), _locate_quote(content, quote, int(line) if line else None))

    if source == "jira":
        key = ref.upper()
        if not research.knows_jira(key):
            return False, f"cites {key}, which you did not read this session — open it with jira_get_issue first", KIND_EVIDENCE_UNREAD, None
        if quote not in _squash(research.jira[key]):
            return False, f"quotes text that is not in {key} as you read it", KIND_EVIDENCE_QUOTE, None
        return True, None, "", None

    page = _PAGE_ID_RE.search(ref)
    page_id = (page.group(1) or page.group(2)) if page else None
    if not page_id or not research.knows_page(page_id):
        return False, f"cites Confluence page {ref}, which you did not read this session — open it with confluence_get_page first", KIND_EVIDENCE_UNREAD, None
    if quote not in _squash(research.pages[page_id]):
        return False, f"quotes text that is not on Confluence page {page_id} as you read it", KIND_EVIDENCE_QUOTE, None
    return True, None, "", None


def verify_evidence(
    draft: dict[str, Any], research: PoResearch, workspace: Workspace,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the draft with `verified` set on every evidence item, the defects for
    the ones that failed). Returns a copy: the caller's draft is untouched.

    Every failure is blocking on the agent's turn — a citation it cannot back
    is the one thing in the draft a person cannot cheaply check — but one that
    survives the last retry is kept, marked `verified: false`, for the person
    to see; they decide what to do with a claim nobody could confirm."""
    annotated = copy.deepcopy(draft)
    defects: list[dict[str, Any]] = []
    groups = [("stories", _stories(annotated)), ("existing", [r for r in _list(annotated.get("existing")) if isinstance(r, dict)])]
    for name, rows in groups:
        for index, row in enumerate(rows):
            label = _text(row.get("ref")) or _text(row.get("key")) or str(index + 1)
            for ev_index, item in enumerate(_list(row.get("evidence"))):
                ok, why, kind, fixed_ref = _check_one_evidence(item, research, workspace)
                if isinstance(item, dict):
                    item["verified"] = ok
                    if fixed_ref:
                        item["ref"] = fixed_ref
                if not ok:
                    ref = _text(item.get("ref")) if isinstance(item, dict) else ""
                    noun = "story" if name == "stories" else "existing story"
                    defects.append(_defect(
                        kind, f"{noun} {label}: evidence {ref or ev_index + 1} {why}",
                        where=f"{name}[{index}].evidence[{ev_index}]",
                    ))
    return annotated, defects


def carry_verification(new_draft: dict[str, Any], old_draft: dict[str, Any] | None) -> dict[str, Any]:
    """`new_draft` (a person's edit) with `verified` carried over from the draft
    it was made from, wherever an evidence item is unchanged. A person cannot
    re-run the checks that need a run's reads, and an item they did not touch
    should not lose its tick for it; one they changed has not been checked."""
    known: dict[tuple[str, str, str], bool] = {}
    for group in ("stories", "existing"):
        for row in _list((old_draft or {}).get(group)):
            for item in _list(row.get("evidence")) if isinstance(row, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("verified"), bool):
                    known[(str(item.get("source")), str(item.get("ref")), str(item.get("quote")))] = item["verified"]
    merged = copy.deepcopy(new_draft)
    for group in ("stories", "existing"):
        for row in _list(merged.get(group)):
            for item in _list(row.get("evidence")) if isinstance(row, dict) else []:
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("source")), str(item.get("ref")), str(item.get("quote")))
                if key in known:
                    item["verified"] = known[key]
                else:
                    item.pop("verified", None)
    return merged


# -- the agent's turn ------------------------------------------------------------------------


def check_draft(
    draft: dict[str, Any], ctx: DraftContext, research: PoResearch, workspace: Workspace,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Everything, for the agent's own turn: (the draft with evidence marked, the
    defects). An answer that is not a draft object at all is judged on that
    alone."""
    integrity = check_draft_integrity(draft, mode=ctx.mode)
    if integrity:
        return draft, integrity
    annotated, evidence = verify_evidence(draft, research, workspace)
    return annotated, [*check_structure(annotated, ctx), *evidence, *kind_warnings(annotated)]


_KIND_HEADINGS = (
    (KIND_MALFORMED_OUTPUT, "Your answer was not a well-formed draft object, so parts of it were lost. Answer with ONE JSON object in which every field is a top-level key holding its declared type — arrays as arrays, never a JSON list inside a string, and never <parameter> or <invoke> tags:"),
    (KIND_WRONG_MODE, "The draft's mode is not this session's:"),
    (KIND_STUB_DRAFT, "These parts are stubs, not content:"),
    (KIND_STUB_FIELD, "These fields are empty, placeholder or invalid:"),
    (KIND_NO_CRITERIA, "These have no acceptance criteria. Write checkable statements — what a reviewer could test:"),
    (KIND_STORY_COUNT, "The number of stories is wrong:"),
    (KIND_LARGE_STORY, "These L stories have no rationale:"),
    (KIND_BAD_REF, "These story refs are not usable:"),
    (KIND_EPIC_REPROPOSED, "The epic is mishandled:"),
    (KIND_DUPLICATE_STORY, "These stories already exist:"),
    (KIND_UNKNOWN_DEPENDENCY, "These dependencies point at nothing:"),
    (KIND_DEPENDENCY_CYCLE, "These dependencies cannot be satisfied:"),
    (KIND_EXISTING_COVERAGE, "The assessment of the existing stories is wrong:"),
    (KIND_EVIDENCE_MISSING, "These evidence references do not resolve — a claim must come from a file, ticket or page you actually opened, so look again before you re-state it:"),
    (KIND_EVIDENCE_UNREAD, "These evidence references name a ticket or page you never opened. Open it with jira_get_issue / confluence_get_page, read it, then quote it — or drop the claim:"),
    (KIND_EVIDENCE_QUOTE, "These evidence quotes are not in the source. A quote is a verbatim excerpt (a sentence or a line), copied from what you read — not a paraphrase:"),
    (KIND_DEPENDS_ON_QA, "Worth fixing:"),
    (KIND_KIND_MISMATCH, "Worth fixing:"),
)


def format_po_feedback(defects: list[dict[str, Any]], *, attempt: int, max_attempts: int) -> str:
    """The correction sent back after a rejected draft. Lead with the count,
    name every defect, say what to do about each — a defect not restated is one
    the next attempt repeats (plan_gate.format_plan_feedback)."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for defect in defects:
        by_kind.setdefault(str(defect.get("kind")), []).append(defect)
    answer_level = by_kind.keys() & {KIND_MALFORMED_OUTPUT}
    lines = [
        "=== YOUR PREVIOUS DRAFT WAS REJECTED ===",
        (
            f"The draft gate found {len(blocking(defects))} blocking problem(s) with the answer itself (attempt {attempt}/{max_attempts})."
            if answer_level else
            f"The draft gate found {len(blocking(defects))} blocking problem(s) (attempt {attempt}/{max_attempts})."
        ),
    ]
    shown_headings: set[str] = set()
    for kind, heading in _KIND_HEADINGS:
        items = by_kind.get(kind) or []
        if not items:
            continue
        if heading not in shown_headings or kind == KIND_MALFORMED_OUTPUT:
            lines += ["", heading]
            shown_headings.add(heading)
        for defect in items[:_MAX_LISTED]:
            lines.append(f"  - {defect['reason']}")
        if len(items) > _MAX_LISTED:
            lines.append(f"  ... and {len(items) - _MAX_LISTED} more")
    lines += [
        "",
        (
            "Answer again: one draft object, every field a top-level key with its declared type, no markup."
            if answer_level else
            "Fix these in the draft and answer again with the whole draft. Anything that was fine stays as it was. "
            "Open the tickets and pages you cite before you quote them: the evidence is checked again on your next answer."
        ),
        "=== END ===",
    ]
    return "\n".join(lines)
