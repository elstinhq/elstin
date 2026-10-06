"""Tier 1 for the *plan*, in the same spirit as elstin/agents/roles/
verifier.py's `compare_to_plan` is Tier 1 for the code: deterministic
checks, no model, run on every plan before it can be approved or handed
to dev.

Why a plan needs a gate at all. Dev is never asked whether it is
finished — the compiler and the planned tests answer, and a failure comes
back as one targeted fix brief (`format_gate_feedback`). The planner had
no equivalent: it stopped when it *believed* it was done, which on a weak
model is after one turn, and nothing between that belief and the pull
request disagreed with it. A plan that named no files passed every later
check vacuously; a plan naming a path that does not exist reached dev,
which then burned its fix rounds discovering that.

Both are answerable from the filesystem, which is why they belong here
rather than in a reviewer's judgement:

- a `files_to_modify` entry names a file that is not in the repo;
- a `files_to_create` entry names one that already is (it is a
  modification, and planning it as a creation invites an overwrite);
- a `discovery_evidence` citation points at a file or a line that does
  not exist — the plan's stated grounds for its claims are checkable, and
  a planner reasoning from files it never read cites lines that are not
  there.

What is deliberately NOT checked: whether each acceptance criterion is
covered by a planned file. The prompt demands it (planner.py's
files_to_modify completeness block) but it is a semantic judgement — the
mapping from "renders the currency" to a file is exactly the thing a
model does and a `os.path.isfile` cannot. Asserting it mechanically would
mean guessing, and a gate that guesses is worse than no gate: it rejects
good plans and teaches the planner to pad its file list.

Ahead of all of that sits `check_plan_integrity`, which asks whether the
answer is a plan object at all — tool-call markup swallowing the fields after
it, an array field that is not an array, a stub summary — on the answer as it
arrived, and stands in for these checks when it finds something, since a
mangled answer fails them for the wrong reason ("names no files").

Multi-repo: every path is resolved through the story's Workspace, so an
`@api/`-prefixed path is checked against the API repo's root and an
unknown label is itself a defect (see elstin/tools/workspace.py).

Two things are fixed rather than sent back (`normalize_plan`), because a
retry spends a whole planner run on what the gate can settle itself: a
path naming another repo without its `@`, and a change in a read-only repo
— moved to `read_only_changes`, for a person to make, so the rest of the
ticket goes ahead instead of the run failing on it. `read_only_changes` is
"left to a person": besides read-only repos it may name a file in a
writable repo the project says agents must not change (a generated,
untracked artifact), which is kept there and flagged, not planned.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from typing import Any

from elstin.tools.grep import DEFAULT_EXCLUDES
from elstin.tools.workspace import REFERENCE_SIGIL, Workspace, slug_label

logger = logging.getLogger("elstin.plan_gate")

KIND_NO_FILES = "no_files"
KIND_NO_CRITERIA = "no_criteria"
KIND_MODIFY_TARGET_MISSING = "modify_target_missing"
KIND_CREATE_TARGET_EXISTS = "create_target_exists"
KIND_UNKNOWN_REPO = "unknown_repo"
KIND_READ_ONLY_REPO = "read_only_repo"
KIND_EVIDENCE_MISSING = "evidence_path_missing"
KIND_QUESTION_PLACEHOLDER = "question_placeholder"
KIND_MALFORMED_OUTPUT = "malformed_output"
KIND_STUB_PLAN = "stub_plan"

# Stub tokens seen in place of real clarification content — a model that
# decides to ask a question before it has one ready has, in practice,
# filled the required schema fields with one of these rather than leaving
# them meaningfully blank.
_PLACEHOLDER_TOKENS = frozenset({"placeholder", "todo", "tbd", "xxx", "n/a", "na", "...", "?", ""})


def _looks_like_placeholder(text: Any) -> bool:
    return str(text or "").strip().strip(".").lower() in _PLACEHOLDER_TOKENS


# Fields the plan schema declares as arrays (planner_loop._PLAN_JSON_SCHEMA).
# `acceptance_criteria` is deliberately not here: a single string in its place
# is a shape `criteria_list` has always tolerated.
_ARRAY_FIELDS = (
    "questions", "files_to_create", "files_to_modify", "read_only_changes", "person_actions", "context_files",
    "discovery_evidence",
)

# Tool-call markup — what a model writes when it falls out of the JSON it was
# asked for and into its own function-call syntax. See check_plan_integrity.
_MARKUP_RE = re.compile(r"</?\s*(?:antml:)?(?:parameter|invoke|function_calls)\b", re.IGNORECASE)
_MARKUP_FIELD_RE = re.compile(r"<\s*(?:antml:)?parameter\s+name\s*=\s*[\"']([\w.-]+)[\"']", re.IGNORECASE)

# Cap on the filename index built to suggest "did you mean". Only built
# when something is already missing, so the common case pays nothing.
_MAX_INDEX_FILES = 20_000
_MAX_SUGGESTIONS = 3

# `path/to/file.ext:123` in discovery_evidence. The leading `/` requirement
# is deliberate: a bare `PriceResponse.java:16` is a shorthand the planner
# uses for a file it read, not a claim about a repo-relative location, and
# flagging it would reject good plans. Only a path that says where it is
# gets checked against where it says.
_CITATION_RE = re.compile(r"(?<![\w/@.])((?:@[\w.-]+/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z]{1,8}):(\d+)")


def _entry_paths(items: Any) -> list[str]:
    paths: list[str] = []
    for item in items or []:
        raw = item.get("path") if isinstance(item, dict) else item
        text = str(raw or "").strip()
        if text:
            paths.append(text)
    return paths


def criteria_list(plan: dict[str, Any]) -> list[str]:
    """The plan's acceptance criteria, tolerating the single-string shape
    a model occasionally returns in place of a list."""
    criteria = plan.get("acceptance_criteria") or []
    if isinstance(criteria, str):
        criteria = [criteria]
    return [str(c) for c in criteria if str(c).strip()]


def _index_by_basename(workspace: Workspace) -> dict[str, list[str]]:
    """{basename: [display path, ...]} across every repo, for suggestions.

    Walked with the same exclusions grep and the codebase skeleton use, so
    a suggestion never points into node_modules or build output."""
    index: dict[str, list[str]] = {}
    count = 0
    for label, root in workspace.read_roots():
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in DEFAULT_EXCLUDES)
            rel_dir = os.path.relpath(dirpath, root)
            for name in filenames:
                rel = name if rel_dir == "." else f"{rel_dir}/{name}".replace(os.sep, "/")
                index.setdefault(name, []).append(workspace.display_path(label, rel))
                count += 1
                if count >= _MAX_INDEX_FILES:
                    return index
    return index


def _suggest(path: str, index: dict[str, list[str]]) -> list[str]:
    return sorted(index.get(os.path.basename(path), []))[:_MAX_SUGGESTIONS]


def _same_file_elsewhere(path: str, index: dict[str, list[str]]) -> str | None:
    """A real file this citation identifies, addressed from a different
    root — `test/utils.tsx` for `pricing-web/src/test/utils.tsx`.

    A planner that has been reading inside a subdirectory cites paths
    relative to it, which is shorthand rather than invention: the file is
    real and the citation names enough of it to be unambiguous. Judging
    that as a phantom cost a genuinely usable plan on the first run of this
    gate, which is the exact failure the module docstring warns about, so
    a suffix match is accepted."""
    needle = path.lstrip("./")
    for candidate in index.get(os.path.basename(path), []):
        if candidate == needle or candidate.endswith("/" + needle):
            return candidate
    return None


def _resolve(workspace: Workspace, path: str) -> tuple[str | None, str | None]:
    """(absolute path, error). The error is set only for a path naming a
    repo the project does not have — which the Workspace refuses to guess
    at, for the reasons in its module docstring."""
    try:
        label, relative = workspace.split(path)
    except Exception as exc:  # noqa: BLE001 — an unknown label is a defect, not a crash
        return None, str(exc)
    root = workspace.roots.get(label)
    if not root:
        return None, f"names repo @{label}, which is not one of this project's repos"
    return os.path.join(root, relative), None


def blocking(defects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The defects that make a plan unusable, as opposed to suspect.

    Only these reject a plan and spend a retry. The split exists because
    the first run of this gate rejected a perfectly usable plan over a
    single `path:line` shorthand in its evidence, failed the story, and had
    the daemon redispatch it — three planner runs' worth of spend for a
    problem that would not have cost dev a thing. A defect blocks when dev
    would act on it (a target that is not there, a new file that already
    is); it warns when it only casts doubt on the plan's reasoning, which
    is a human's call at the approval gate."""
    return [d for d in defects if d.get("blocking", True)]


def check_questions(questions: list[Any]) -> list[dict[str, Any]]:
    """Degenerate `questions` content — stub text a model emits when it
    decides to ask before it has an actual question ready.

    A clarification round-trip skips `check_plan` entirely (see
    planner_loop.py): the plan's files/criteria are legitimately empty
    until the human answers, so nothing else validates what `questions`
    actually contains. That let a plan through with every field —
    `question`, `context`, an option's `label` — literally the string
    "placeholder", which then sat in front of a human as if it were a real
    question. This only catches that: an exact stub token, not a
    judgement about whether the question is a *good* one."""
    defects: list[dict[str, Any]] = []
    for q in questions or []:
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        question_text = q.get("question")
        if _looks_like_placeholder(question_text):
            defects.append({
                "kind": KIND_QUESTION_PLACEHOLDER, "path": None, "suggestions": [],
                "reason": f"clarification question {qid!r} has stub text ({question_text!r}) instead of an actual question",
            })
        for opt in q.get("options") or []:
            label = opt.get("label") if isinstance(opt, dict) else opt
            if _looks_like_placeholder(label):
                defects.append({
                    "kind": KIND_QUESTION_PLACEHOLDER, "path": None, "suggestions": [],
                    "reason": f"clarification question {qid!r} has an option with a stub label ({label!r})",
                })
    return defects


def _string_values(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """(path, text) for every string anywhere in `value`, outermost first."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield from _string_values(inner, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _string_values(inner, f"{path}[{index}]")


def _json_type(value: Any) -> str:
    return {str: "string", dict: "object", bool: "boolean", int: "number", float: "number"}.get(type(value), type(value).__name__)


def markup_defects(answer: dict[str, Any], *, noun: str = "plan") -> list[dict[str, Any]]:
    """Tool-call markup inside any string of a structured answer, naming the
    fields it swallowed — one defect per top-level field, at most three.

    Shared with elstin/agents/roles/po_gate.py: any role whose answer is a
    structured object can lose its later fields to a model that slips into its
    own function-call syntax, and the symptom is the same."""
    defects: list[dict[str, Any]] = []
    reported: set[str] = set()
    for path, text in _string_values(answer):
        match = _MARKUP_RE.search(text)
        field = re.split(r"[.\[]", path, maxsplit=1)[0]
        if not match or field in reported or len(reported) >= 3:
            continue
        reported.add(field)
        swallowed = list(dict.fromkeys(_MARKUP_FIELD_RE.findall(text)))
        reason = f'"{path}" holds tool-call markup ({match.group(0).strip()}…) instead of plain text'
        if swallowed:
            reason += f", so the fields written after it were swallowed into that value and are missing from the {noun}: {', '.join(swallowed)}"
        defects.append({"kind": KIND_MALFORMED_OUTPUT, "path": None, "suggestions": [], "reason": reason})
    return defects


def array_defects(answer: dict[str, Any], fields: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """Each of `fields` that is present but is not an array. A field that is
    simply absent is not judged here."""
    defects: list[dict[str, Any]] = []
    for key in fields:
        value = answer.get(key)
        if value is None or isinstance(value, list):
            continue
        hint = " (a JSON list written inside a string)" if isinstance(value, str) and value.lstrip().startswith("[") else ""
        defects.append({
            "kind": KIND_MALFORMED_OUTPUT, "path": None, "suggestions": [],
            "reason": f'"{key}" must be a JSON array but is a {_json_type(value)}{hint}',
        })
    return defects


def looks_like_placeholder(text: Any) -> bool:
    """Stub text a model writes to buy time ("TBD", "todo", "...")."""
    return _looks_like_placeholder(text)


def check_plan_integrity(plan: dict[str, Any], *, clarifying: bool = False) -> list[dict[str, Any]]:
    """Whether `plan` is a plan object at all, ahead of whether it is a good one.

    Run on the provider's answer exactly as it arrived — before
    `normalize_plan`, which iterates the file lists and would turn a string
    where an array belongs into one entry per character — and, when it finds
    anything, instead of `check_plan` rather than beside it. A mangled answer
    fails `check_plan` too, but for the wrong reason: "the plan names no files"
    sent the planner back to look for files it had already named.

    The failure this exists for is a run on `claude-sonnet-5` behind an
    OpenAI-compatible proxy. The structured answer parsed as JSON, but its
    first value was one long string holding `["..."]\\n<parameter
    name="architecture">{...}<parameter name="files_to_modify">...` — the
    model had slipped into its own tool-call syntax, so every field after the
    first was inside that string and none was a field of the plan. The gate
    saw empty file lists, said so, and the retries came back as stubs. Three
    things are checked, each of which is blocking:

    - tool-call markup inside any value, naming the fields it swallowed;
    - an array field that is not an array;
    - a `feature_summary` that is stub text. Not applied to a clarification
      round-trip (`clarifying`): there the plan fields are allowed to be thin
      until a person answers (planner_loop.py), and `check_questions` judges
      the part that reaches them.

    A field that is simply absent is not judged here; an empty plan is
    `check_plan`'s to report."""
    defects: list[dict[str, Any]] = markup_defects(plan)
    defects.extend(array_defects(plan, _ARRAY_FIELDS))

    if not clarifying and "feature_summary" in plan:
        summary = plan["feature_summary"]
        if isinstance(summary, str) and _looks_like_placeholder(summary.strip().strip("#*_`-: \t\n")):
            defects.append({
                "kind": KIND_STUB_PLAN, "path": None, "suggestions": [],
                "reason": f"feature_summary is stub text ({summary.strip()[:40]!r}), not a description of the plan",
            })
    return defects


def plan_shape(plan: dict[str, Any]) -> dict[str, Any]:
    """What the provider's answer was made of, small enough to keep: the
    fields it had and how much each held. Recorded when the gate rejects a
    plan, because the activity feed cuts the plan's own text at 4,000
    characters — well short of the file lists, which come after the
    acceptance criteria — and a rejection that cannot say whether the lists
    were empty or never arrived cannot be diagnosed from the feed."""

    def size(key: str) -> Any:
        if key not in plan:
            return "missing"
        value = plan[key]
        if value is None:
            return "null"
        if isinstance(value, list):
            return len(value)
        if isinstance(value, str):
            return f"string ({len(value)} chars)"
        return _json_type(value)

    shape: dict[str, Any] = {
        "fields": sorted(str(key) for key in plan),
        **{key: size(key) for key in ("files_to_create", "files_to_modify", "read_only_changes", "acceptance_criteria", "questions")},
    }
    summary = plan.get("feature_summary")
    shape["feature_summary"] = summary.strip()[:40] if isinstance(summary, str) else size("feature_summary")
    return shape


def describe_plan_shape(shape: dict[str, Any]) -> str:
    return (
        "Received: "
        + ", ".join(f"{key} {shape[key]}" for key in ("files_to_create", "files_to_modify", "read_only_changes", "acceptance_criteria", "questions"))
        + f"; feature_summary {shape['feature_summary']!r}; fields present: {', '.join(shape['fields']) or 'none'}"
    )


def person_actions_of(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The plan's `person_actions` as `[{action, reason}]`, whatever shape the
    model left them in: a bare string is an action with no reason, an entry
    with no action text is dropped, and a repeat is kept once. A plan from
    before the field existed has none.

    Actions are things the ticket's acceptance criteria lean on that no agent
    here can do — see PLANNER_SYSTEM_CORE's "what the dev agent can and cannot do":
    open pull requests and branches it cannot see, data it has no access to,
    a publication or a sign-off. They are the non-file counterpart of
    `read_only_changes`, and travel the same way: the plan's reader is told,
    the developer is told not to attempt them, and the reviewer never fails a
    run for their absence."""
    raw = plan.get("person_actions")
    if not isinstance(raw, list):
        return []
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("action") or "").strip()
            reason = str(item.get("reason") or "").strip() or None
        else:
            text, reason = str(item or "").strip(), None
        if not text or text in seen:
            continue
        seen.add(text)
        actions.append({"action": text, "reason": reason})
    return actions


def normalize_plan(plan: dict[str, Any], workspace: Workspace) -> tuple[dict[str, Any], list[str]]:
    """The plan with two fixes made instead of sent back. Returns (plan,
    notes), one note per fix, for the activity feed.

    - A path naming another repo without its `@` — `mono-adminportal/src/x.ts`
      where `mono-adminportal` is one of the project's repos, not a folder of
      the default one — becomes `@mono-adminportal/src/x.ts`. Sent back, a
      planner fixing one path tends to break another (one story's retry
      dropped every `@` and then had eight "missing" files).
    - A file to create or modify in a read-only repo or reference folder is
      moved to `read_only_changes`: a change the ticket needs that elstin may
      not make and a person has to. A modified one is added to
      context_files, since it still matters to the plan. Rejecting it
      instead failed a ticket that genuinely needed an OpenAPI contract
      change in a contract repo marked read-only — three rejected plans,
      and the rest of the work never started.

    A `read_only_changes` entry in a repo elstin CAN write stays there — the
    planner is only to do that for a file the project says agents must not
    change — and is noted, for the person approving the plan to check.
    Moving it back into the plan undid exactly that: one story's planner left
    `bundled-oas.yaml`, an untracked copy of the contract, to a
    person as the workspace's CLAUDE.md says, and was overruled into
    hand-writing it. A path the plan also lists as a file to change is
    planned work, and leaves this list."""
    plan = dict(plan)
    notes: list[str] = []

    def with_prefix(path: str) -> str:
        text = str(path or "").strip()
        if not text or text.startswith(REFERENCE_SIGIL) or not workspace.is_multi_repo:
            return text
        head, sep, rest = text.partition("/")
        label = slug_label(head)
        if not sep or not rest or label == workspace.default_label or label not in workspace.roots:
            return text
        if os.path.exists(os.path.join(workspace.default_root, head)):
            return text  # a real folder of the default repo, which happens to share the name
        fixed = f"{REFERENCE_SIGIL}{label}/{rest}"
        notes.append(f"{text} is in another repo: read as {fixed}")
        return fixed

    for key in ("files_to_create", "files_to_modify", "read_only_changes"):
        entries = []
        for entry in plan.get(key) or []:
            if isinstance(entry, dict):
                entries.append({**entry, "path": with_prefix(entry.get("path") or "")})
            else:
                entries.append(with_prefix(entry))
        plan[key] = entries
    plan["context_files"] = [with_prefix(p) for p in plan.get("context_files") or []]

    def label_of(path: str) -> str | None:
        try:
            return workspace.split(path)[0]
        except Exception:  # noqa: BLE001 — an unknown label is check_plan's to report
            return None

    read_only = [dict(e) if isinstance(e, dict) else {"path": e, "purpose": None} for e in plan.get("read_only_changes") or []]
    planned = {entry.get("path") if isinstance(entry, dict) else entry
               for key in ("files_to_create", "files_to_modify") for entry in plan.get(key) or []}
    kept_read_only = []
    for entry in read_only:
        if entry["path"] in planned:
            continue
        if label_of(entry["path"]) not in workspace.read_only:
            notes.append(
                f"{entry['path']} is in a repo elstin can change, but the plan leaves it to a person — check that "
                "at approval"
            )
        kept_read_only.append(entry)
    for key, action in (("files_to_create", "create"), ("files_to_modify", "modify")):
        kept = []
        for entry in plan.get(key) or []:
            path = entry.get("path") if isinstance(entry, dict) else entry
            if label_of(path) in workspace.read_only:
                purpose = entry.get("purpose") if isinstance(entry, dict) else None
                if all(e["path"] != path for e in kept_read_only):
                    kept_read_only.append({"path": path, "purpose": purpose})
                if action == "modify" and path not in plan["context_files"]:
                    plan["context_files"].append(path)
                notes.append(f"{path} is in a read-only repo: listed for a person to change, not planned")
            else:
                kept.append(entry)
        plan[key] = kept
    plan["read_only_changes"] = kept_read_only
    plan["person_actions"] = person_actions_of(plan)
    return plan, notes


def check_plan(plan: dict[str, Any], workspace: Workspace) -> list[dict[str, Any]]:
    """Every defect in `plan`, as `{kind, path, reason, suggestions,
    blocking}`. An empty list means the plan is usable — not that it is
    right, which is what the human gate and the code-side Tier 1 gate are
    for. Pass the result through `blocking()` before rejecting on it."""
    defects: list[dict[str, Any]] = []
    creates = _entry_paths(plan.get("files_to_create"))
    modifies = _entry_paths(plan.get("files_to_modify"))

    if not creates and not modifies:
        defects.append({
            "kind": KIND_NO_FILES, "path": None, "suggestions": [],
            "reason": (
                "every change this plan makes is in a read-only repo (read_only_changes) — plan what elstin can "
                "change for this ticket, or, if nothing, ask a question saying so"
                if plan.get("read_only_changes") else "the plan names no files to create and none to modify"
            ),
        })
    if not criteria_list(plan):
        defects.append({
            "kind": KIND_NO_CRITERIA, "path": None, "suggestions": [],
            "reason": "the plan states no acceptance criteria",
        })

    # Built once, and only if something is already wrong — the walk is the
    # expensive part of this module and a good plan should not pay for it.
    index: dict[str, list[str]] | None = None

    def index_for() -> dict[str, list[str]]:
        nonlocal index
        if index is None:
            index = _index_by_basename(workspace)
        return index

    def suggestions_for(path: str) -> list[str]:
        return _suggest(path, index_for())

    # A read-only repo or reference folder is context, never a target
    # (elstin/tools/workspace.py's Workspace.read_only). Checked first and
    # alone: whether such a path exists is beside the point.
    for path in [*modifies, *creates]:
        try:
            label, _relative = workspace.split(path)
        except Exception:  # noqa: BLE001 — an unknown label is reported below
            continue
        if label in workspace.read_only:
            defects.append({
                "kind": KIND_READ_ONLY_REPO, "path": path, "suggestions": [],
                "reason": f"{path} is in @{label}, which is read-only — reference it in context_files, never change it",
            })
    read_only_paths = {d["path"] for d in defects if d["kind"] == KIND_READ_ONLY_REPO}
    modifies = [p for p in modifies if p not in read_only_paths]
    creates = [p for p in creates if p not in read_only_paths]

    seen_missing: set[str] = set()
    for path in modifies:
        abs_path, error = _resolve(workspace, path)
        if error:
            defects.append({"kind": KIND_UNKNOWN_REPO, "path": path, "reason": error, "suggestions": []})
            continue
        if not os.path.isfile(abs_path):
            if path in seen_missing:
                continue
            seen_missing.add(path)
            defects.append({
                "kind": KIND_MODIFY_TARGET_MISSING, "path": path,
                "reason": f"{path} is listed under files_to_modify but no such file exists",
                "suggestions": suggestions_for(path),
            })

    for path in creates:
        abs_path, error = _resolve(workspace, path)
        if error:
            defects.append({"kind": KIND_UNKNOWN_REPO, "path": path, "reason": error, "suggestions": []})
            continue
        if os.path.isfile(abs_path):
            defects.append({
                "kind": KIND_CREATE_TARGET_EXISTS, "path": path,
                "reason": f"{path} is listed under files_to_create but already exists",
                "suggestions": [],
            })

    defects.extend(_check_evidence(plan, workspace, index_for))
    return defects


def _check_evidence(
    plan: dict[str, Any], workspace: Workspace, index_for: Any
) -> list[dict[str, Any]]:
    """`path:line` citations in discovery_evidence that do not resolve.

    A planner reasoning from files it never read cites lines that are not
    there, and that is the one part of its reasoning a machine can audit.
    These are WARNINGS, not blockers (see `blocking`): a wrong citation
    makes a plan's stated grounds suspect, but it does not make the plan
    unusable the way a missing dev target does — and rejecting an otherwise
    good plan over one costs a whole planner run. The human reading the
    plan at the approval gate is the right audience for "this plan cites
    two files that do not exist".

    Only citations carrying a directory are judged (see _CITATION_RE), and
    a path that identifies a real file from a different root is accepted
    (see _same_file_elsewhere)."""
    defects: list[dict[str, Any]] = []
    reported: set[str] = set()
    for item in plan.get("discovery_evidence") or []:
        for path, line_text in _CITATION_RE.findall(str(item)):
            if path in reported:
                continue
            abs_path, error = _resolve(workspace, path)
            if error:
                reported.add(path)
                defects.append({
                    "kind": KIND_EVIDENCE_MISSING, "path": path, "blocking": False, "suggestions": [],
                    "reason": f"discovery_evidence cites {path}:{line_text}, and {error}",
                })
                continue
            if not os.path.isfile(abs_path):
                elsewhere = _same_file_elsewhere(path, index_for())
                if elsewhere is None:
                    reported.add(path)
                    defects.append({
                        "kind": KIND_EVIDENCE_MISSING, "path": path, "blocking": False,
                        "reason": f"discovery_evidence cites {path}:{line_text}, but no such file exists",
                        "suggestions": _suggest(path, index_for()),
                    })
                    continue
                # Real file, addressed from a subdirectory — judge the line
                # against the file it actually names.
                resolved, _ = _resolve(workspace, elsewhere)
                abs_path = resolved or abs_path
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as handle:
                    total = sum(1 for _ in handle)
            except OSError:
                continue
            if int(line_text) > total:
                reported.add(path)
                defects.append({
                    "kind": KIND_EVIDENCE_MISSING, "path": path, "blocking": False, "suggestions": [],
                    "reason": f"discovery_evidence cites {path}:{line_text}, but that file has only {total} line(s)",
                })
    return defects


_MAX_LISTED = 12

_KIND_HEADINGS = (
    (KIND_MODIFY_TARGET_MISSING, "These files_to_modify entries do not exist. Find the real path with find_files_tool/grep_tool, or move the entry to files_to_create if the file genuinely has to be new:"),
    (KIND_CREATE_TARGET_EXISTS, "These files_to_create entries already exist. Read them and move them to files_to_modify — planning an existing file as new invites an overwrite of working code:"),
    (KIND_EVIDENCE_MISSING, "These discovery_evidence citations do not resolve. Every claim in the plan must come from a file you actually read, so re-read before you re-state:"),
    (KIND_UNKNOWN_REPO, "These paths name a repo this project does not have. Use the exact `@label/` prefixes listed in your prompt:"),
    (KIND_READ_ONLY_REPO, "These paths are in a read-only repo or reference folder. Read them for context (context_files), but plan the change where it belongs — and if the ticket truly needs one there, say so in reasoning for a person to make:"),
    (KIND_MALFORMED_OUTPUT, "Your answer was not a well-formed plan object, so parts of it were lost. Answer with ONE JSON object in which every field is a top-level key holding its declared type — arrays as arrays, never a JSON list inside a string, and never <parameter> or <invoke> tags:"),
    (KIND_STUB_PLAN, "Your answer was a stub, not a plan. Do the investigation first (find_files_tool, grep_tool, read_file_tool), then write the real plan — never answer with placeholder text to buy time:"),
    (KIND_QUESTION_PLACEHOLDER, "These clarification questions are stub placeholders, not real questions. Only ask a question when you have a concrete, project-grounded question and options to offer; otherwise leave `questions` empty and produce the full plan:"),
)


def format_plan_feedback(defects: list[dict[str, Any]], *, attempt: int, max_attempts: int) -> str:
    """The correction appended to the planner's prompt on a retry.

    The planner's retry starts from a fresh history — it keeps nothing
    from the rejected attempt — so a defect that is not restated here is a
    defect the next attempt repeats. Modelled on
    verifier.format_gate_feedback: lead with the count, name every file,
    say what to do about each, and end with the one instruction that
    matters."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    general: list[dict[str, Any]] = []
    for defect in defects:
        kind = defect.get("kind")
        if kind in (KIND_NO_FILES, KIND_NO_CRITERIA):
            general.append(defect)
        else:
            by_kind.setdefault(str(kind), []).append(defect)

    # A mangled or stub answer is rejected before any check against the
    # repository runs, and says nothing of the files — so it must not be
    # worded as though they were looked at.
    answer_defects = by_kind.keys() & {KIND_MALFORMED_OUTPUT, KIND_STUB_PLAN}
    lines = [
        "=== YOUR PREVIOUS PLAN WAS REJECTED ===",
        (
            f"The plan gate found {len(defects)} problem(s) with the answer itself (plan attempt {attempt}/{max_attempts})."
            if answer_defects else
            f"Checks against the repository found {len(defects)} problem(s) (plan attempt {attempt}/{max_attempts})."
        ),
    ]

    if general:
        lines.append("")
        lines.extend(f"- {d['reason'].capitalize()}." for d in general)
        lines.append(
            "A plan must name every file to create or modify and state the acceptance criteria the "
            "finished work is checked against. The dev agent may only write files this plan names, so "
            "an empty plan ships the ticket as an empty change. If the feature looks already "
            "implemented, that is still a plan: list the existing files that implement it, and the "
            "existing tests that prove it, under files_to_modify with a purpose starting "
            "\"Already satisfied:\". The dev agent confirms each needs no edit and the story finishes "
            "as done with no change. Do not invent an edit to fill the list."
        )

    for kind, heading in _KIND_HEADINGS:
        items = by_kind.get(kind) or []
        if not items:
            continue
        lines += ["", heading]
        for defect in items[:_MAX_LISTED]:
            lines.append(f"  - {defect['reason']}")
            for suggestion in defect.get("suggestions") or []:
                lines.append(f"      a file of that name exists at: {suggestion}")
        if len(items) > _MAX_LISTED:
            lines.append(f"  ... and {len(items) - _MAX_LISTED} more")

    lines += [
        "",
        (
            "Produce the plan JSON again: one object, every field a top-level key with its declared type, "
            "no markup, and real content in place of any stub."
            if answer_defects else
            "Verify each path with your find_files_tool/grep_tool/read_file_tool before you name it — a "
            "path you have not confirmed is a guess, and this check runs again on your next answer. "
            "Then produce the plan JSON again, corrected."
        ),
        "=== END ===",
    ]
    return "\n".join(lines)
