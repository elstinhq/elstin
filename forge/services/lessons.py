"""Turning a finished story's trouble into candidate lessons.

A story that needed retries — the gate sent dev back, the reviewer rejected
it, the merge queue returned it, a person retried it — cost tokens to find
something out that the next story will find out again. This reads that
story's own evidence once, after it lands, and proposes a few short lessons
for the project guide (forge/db/models/lesson.py). Nothing here reaches an
agent: a person accepts each one first, because the guide is read as
authoritative and a wrong line gets followed.

One single-shot call through `generation.chat_completion_json`, never an
agent loop (AGENTS.md hard constraint #9) — there are no tools and no
multi-turn state, and the evidence is already gathered.

Guards against a plausible-sounding invention, which is the failure that
matters here:
- a story with no friction is not mined at all (no call, no cost);
- every lesson must quote the evidence, and a quote that is not in the
  evidence drops the lesson;
- a lesson already known — in any status, rejected ones included — is
  counted, not re-proposed.

Best-effort like the project guide: it is context for later runs, so a
failure is logged and skipped, never a failed merge.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from typing import Any

from forge.config.store import get_config, resolve_bool_config
from forge.llm.registry import LlmNotConfiguredError
from forge.services.generation import GenerationError, chat_completion_json
from forge.services.run_activity import list_activity
from forge.services.usage import get_run_outcome

logger = logging.getLogger("forge.lessons")

MAX_LESSONS_PER_STORY = 3
MIN_LESSON_CHARS = 20
MAX_LESSON_CHARS = 300
# The evidence handed to the model. Activity feeds are long (every tool call
# is one); only the status lines carry what happened, and the newest are the
# ones that describe how the run ended.
MAX_EVIDENCE_CHARS = 12_000
_EVENT_DETAIL_CHARS = 600
_MAX_EXISTING_LESSONS = 40
_KINDS = ("environment", "convention", "gotcha", "process")

_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "array",
            "maxItems": MAX_LESSONS_PER_STORY,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "scope_globs": {"type": "array", "items": {"type": "string"}},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["text", "kind", "scope_globs", "evidence_quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lessons"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = f"""You read the record of one finished story from an autonomous coding pipeline and extract lessons that would have saved it a retry. The lessons go into the project's AGENTS.md-style guide, which future planner and dev agents treat as authoritative.

Rules:
- Return at most {MAX_LESSONS_PER_STORY} lessons, and return none when nothing in the record is a reusable lesson. An empty list is a good answer.
- A lesson is about THIS project's repos, toolchain or conventions, stated as an instruction ("Build with JDK 21; the default JDK 26 crashes javac"). Never about this one ticket, never generic engineering advice.
- Each lesson must be supported by the record. "evidence_quote" is a short passage copied verbatim from the record; a lesson whose quote is not in the record is discarded.
- One line each, {MIN_LESSON_CHARS}-{MAX_LESSON_CHARS} characters. No preamble, no reasoning.
- Do not repeat a lesson from "Already known", even reworded.
- "scope_globs": path globs the lesson applies to (e.g. "src/**/*.vue"), or an empty list when it holds for the whole project.
- kind: environment (toolchain/build/runtime), convention (how code here is written), gotcha (a trap that failed a run), process (how work should be ordered or split)."""


def lessons_enabled(project_id: int | None) -> bool:
    return resolve_bool_config("capture_lessons", project_id)


def had_friction(story: dict, outcome: dict | None) -> bool:
    """Whether the run went anywhere worth learning from. Absent counters
    (a run that predates them) count as none: "not measured" must not read
    as trouble."""
    counters = outcome or {}
    return bool(
        (counters.get("gate_failures") or 0)
        or (counters.get("verifier_rejections") or 0)
        or (story.get("merge_attempts") or 0)
        or (story.get("retry_count") or 0)
    )


def build_evidence(story: dict, outcome: dict | None, events: list[dict[str, Any]]) -> str:
    """The record the model reads: what the run measured, what a person told
    it, and its status lines (newest kept when it must be cut)."""
    lines = [f"Story {story['jira_key']}: {story.get('summary') or ''}".strip()]
    counters = {k: v for k, v in (outcome or {}).items() if k not in ("run_id",) and v not in (None, 0)}
    if counters:
        lines.append("Run counters: " + ", ".join(f"{k}={v}" for k, v in sorted(counters.items())))
    for name in ("merge_attempts", "retry_count", "failure_stage", "parked_reason"):
        if story.get(name):
            lines.append(f"{name}: {story[name]}")
    answers = story.get("clarification_answers") or []
    if answers:
        lines.append("A person's answers to the planner's questions: " + " | ".join(str(a)[:300] for a in answers))
    lines.append("")
    lines.append("Status events, oldest first:")
    status = []
    for event in events:
        if event.get("type") != "status":
            continue
        entry = f"[{event.get('phase')}] {event.get('title')}"
        detail = (event.get("detail") or "").strip()
        if detail:
            entry += f" — {detail[:_EVENT_DETAIL_CHARS]}"
        status.append(entry)
    head = "\n".join(lines)
    budget = max(0, MAX_EVIDENCE_CHARS - len(head))
    kept: list[str] = []
    used = 0
    for entry in reversed(status):
        if used + len(entry) + 1 > budget:
            break
        kept.append(entry)
        used += len(entry) + 1
    kept.reverse()
    return head + "\n" + "\n".join(kept)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def validate_lessons(raw: Any, evidence: str) -> list[dict[str, Any]]:
    """The model's lessons that are usable: well-formed, sized as asked, and
    quoting the evidence. Everything else is dropped, silently — an
    unverifiable lesson is exactly the one not to show a reviewer."""
    if not isinstance(raw, list):
        return []
    haystack = _squash(evidence)
    kept: list[dict[str, Any]] = []
    for item in raw[:MAX_LESSONS_PER_STORY]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        quote = _squash(str(item.get("evidence_quote") or ""))
        if not (MIN_LESSON_CHARS <= len(text) <= MAX_LESSON_CHARS):
            continue
        if not quote or quote not in haystack:
            continue
        kind = item.get("kind") if item.get("kind") in _KINDS else "gotcha"
        globs = [str(g).strip() for g in (item.get("scope_globs") or []) if str(g).strip()][:5]
        kept.append({"text": text, "kind": kind, "scope_globs": globs, "evidence_quote": str(item["evidence_quote"]).strip()})
    return kept


def _lesson_model() -> str | None:
    """The planner's model, like the guide's: the lessons end up in the same
    authoritative document, and the small verifier/QA model is chosen for
    pass/fail review, not for writing instructions. None falls back to that
    default when no planner model is set."""
    return (get_config("planner_sdk_model") or "").strip() or None


async def capture_story_lessons(sm, epic: dict, story: dict) -> list[dict]:
    """Proposes lessons from one landed story; returns the rows newly
    created (a re-proposed one is counted, not returned). Never raises."""
    try:
        project_id = epic.get("project_id")
        if project_id is None or not lessons_enabled(project_id):
            return []
        story_key = story["jira_key"]
        outcome = await asyncio.to_thread(get_run_outcome, story_key)
        if not had_friction(story, outcome):
            return []

        events = await asyncio.to_thread(list_activity, story_key)
        evidence = build_evidence(story, outcome, events)
        known = await asyncio.to_thread(sm.list_project_lessons, project_id)
        known_block = "\n".join(f"- {row['text']}" for row in known[-_MAX_EXISTING_LESSONS:]) or "(none)"

        try:
            parsed, _usage = await chat_completion_json(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Already known:\n{known_block}\n\nRecord:\n{evidence}"},
                ],
                response_schema=_SCHEMA,
                response_schema_name="project_lessons",
                # No run_id: this runs after the story landed, so it is not
                # the story's spend, and a story that used its whole ceiling
                # should still be learned from.
                label="lesson extraction",
                model=_lesson_model(),
            )
        except (GenerationError, LlmNotConfiguredError) as exc:
            logger.warning("lessons: not captured for %s: %s", story_key, exc)
            return []

        created: list[dict] = []
        for lesson in validate_lessons((parsed or {}).get("lessons"), evidence):
            row = await asyncio.to_thread(
                sm.add_lesson_candidate,
                project_id,
                lesson["text"],
                project_repo_id=story.get("effective_project_repo_id"),
                kind=lesson["kind"],
                scope_globs=lesson["scope_globs"],
                story_key=story_key,
                evidence_quote=lesson["evidence_quote"],
            )
            if row.get("created"):
                created.append(row)
        if created:
            logger.info("lessons: %d proposed from %s", len(created), story_key)
        return created
    except Exception:  # noqa: BLE001 — see the module docstring
        logger.exception("lessons: could not capture from %s", story.get("jira_key"))
        return []


# --- handing accepted lessons to an agent -------------------------------
# Only `accepted` lessons ever get here (project_guide_for_run reads that
# status and no other). Kept apart from the guide's own text: the guide is
# the repo's or forge's summary of the code, these are what a person vouched
# for after watching forge stumble, and the prompt says which is which.

# Its own budget, so lessons never crowd out the guide the way a long guide
# would crowd out them — and a standing tax on every planner turn stays small.
MAX_LESSONS_CHARS = 2_000


def _glob_matches(glob: str, path: str) -> bool:
    """fnmatch, where `**/` may match no directory (so `src/**/*.vue` also
    covers `src/App.vue`). A path may carry a `@label/` repo prefix; a glob
    is tried against it with and without."""
    bare = path.split("/", 1)[1] if path.startswith("@") and "/" in path else path
    patterns = {glob, glob.replace("**/", "")}
    return any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns for candidate in (path, bare))


def select_lessons(lessons: list[dict], paths: list[str] | None = None) -> list[dict]:
    """The lessons an agent should be shown, within MAX_LESSONS_CHARS.

    `paths` is what the reader will touch (dev: the plan's files). None
    means "no narrowing" — the planner, which does not yet know — and every
    lesson qualifies. Given paths, an unscoped lesson always qualifies and a
    scoped one only when a glob matches one of them. Most-confirmed first,
    so when the budget cuts, what is dropped is what one story alone saw."""
    chosen = []
    for lesson in lessons:
        globs = lesson.get("scope_globs") or []
        if paths is None or not globs or any(_glob_matches(g, p) for g in globs for p in paths):
            chosen.append(lesson)
    chosen.sort(key=lambda row: (-(row.get("occurrences") or 1), row.get("id") or 0))
    kept: list[dict] = []
    used = 0
    for lesson in chosen:
        cost = len(lesson["text"]) + 3
        if used + cost > MAX_LESSONS_CHARS:
            break
        kept.append(lesson)
        used += cost
    return kept


def format_lessons_block(lessons: list[dict] | None, paths: list[str] | None = None) -> str:
    """The prompt section for these lessons, or "" when none qualify."""
    selected = select_lessons(lessons or [], paths)
    if not selected:
        return ""
    return (
        "Lessons learned in this project — a person reviewed and accepted each after earlier runs went wrong. "
        "Follow them; where one disagrees with what you read in the repository, the repository is right.\n"
        + "\n".join(f"- {lesson['text']}" for lesson in selected)
        + "\n\n"
    )


def plan_paths(plan: dict | None) -> list[str]:
    """Every file an approved plan creates or modifies."""
    paths: list[str] = []
    for key in ("files_to_create", "files_to_modify"):
        for entry in (plan or {}).get(key) or []:
            raw = entry.get("path") if isinstance(entry, dict) else entry
            if raw and str(raw).strip():
                paths.append(str(raw).strip())
    return paths
