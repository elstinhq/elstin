"""What is waiting on a person, across projects — the UI's "Needs you".

Every gate elstin stops at for a human already exists somewhere on the board:
a plan to approve or questions to answer (a story), a pull request to open or
keep local (an epic), a ticket whose text changed under a plan, a run that
failed and is not retrying by itself, a proposed lesson, a backlog draft that
is ready or needs answers. This module only gathers them into one list, so the
sidebar can count them and the dashboard can show them in one place. It decides
nothing and changes nothing; each item's action goes through the route that
already owns it.

Reads go through StateManager only (AGENTS.md hard constraint #1).
"""
from __future__ import annotations

from typing import Any

from elstin.state.manager import StateManager

# One per gate; the UI groups by these.
PLAN_APPROVAL = "plan_approval"
QUESTIONS = "questions"
PR_APPROVAL = "pr_approval"
EXTERNAL_CHANGE = "external_change"
FAILED = "failed"
LESSON = "lesson"
PO_DRAFT = "po_draft"

_DONE = {"merged", "done"}
# A draft a person has to act on: answer the agent, or review and publish.
_PO_WAITING = {"awaiting_input", "ready"}


def _display_key(row: dict) -> str:
    return row.get("external_id") or row["jira_key"]


# Why a story failed, in a few words. A parked reason says it best
# (cancelled, …); otherwise the stage it stopped in.
_FAILURE_STAGE = {"run": "run failed", "merge": "merge into the epic failed"}


def _failure_detail(story: dict) -> str:
    reason = story.get("parked_reason")
    if reason:
        return reason.replace("_", " ")
    stage = story.get("failure_stage")
    return _FAILURE_STAGE.get(stage, f"failed in {stage}" if stage else "run failed")


def _story_items(project_id: int, epic: dict, story: dict) -> list[dict[str, Any]]:
    """The gates one top-level story is stopped at. Sub-tasks are never
    dispatched, so none of their states is a person's to act on."""
    base = {
        "project_id": project_id,
        "epic_key": epic["jira_key"],
        "story_key": story["jira_key"],
        "key": _display_key(story),
        "title": story.get("summary") or "",
    }
    items: list[dict[str, Any]] = []
    status = story.get("status")
    if status == "awaiting_plan_approval" and not story.get("gate_approved"):
        questions = story.get("clarification_questions") or []
        if questions:
            items.append({**base, "kind": QUESTIONS, "detail": f"{len(questions)} question{'s' if len(questions) != 1 else ''}"})
        else:
            items.append({**base, "kind": PLAN_APPROVAL, "detail": "plan ready"})
    if story.get("external_pending"):
        reasons = (story["external_pending"] or {}).get("reasons") or []
        items.append({**base, "kind": EXTERNAL_CHANGE, "detail": "; ".join(reasons) or "changed in the source"})
    # A failed story with a retry time is elstin's to retry (a provider limit,
    # an unreachable endpoint), not a person's — until that time passes and it
    # is still failed, which the daemon turns back into a run anyway.
    if status == "failed" and not story.get("retry_at"):
        items.append({**base, "kind": FAILED, "detail": _failure_detail(story)})
    return items


def _project_items(sm: StateManager, project_id: int, epics: list[dict]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for epic in epics:
        stories = [s for s in sm.list_stories_for_epic(epic["jira_key"]) if not s.get("parent_key")]
        if epic.get("pr_approval") == "awaiting":
            merged = sum(1 for s in stories if s.get("status") in _DONE)
            items.append({
                "kind": PR_APPROVAL, "project_id": project_id, "epic_key": epic["jira_key"], "story_key": None,
                "key": _display_key(epic), "title": epic.get("summary") or "", "detail": f"{merged}/{len(stories)} merged",
            })
        for story in stories:
            items.extend(_story_items(project_id, epic, story))

    for lesson in sm.list_project_lessons(project_id, status="proposed"):
        items.append({
            "kind": LESSON, "project_id": project_id, "epic_key": None, "story_key": None, "key": None,
            "lesson_id": lesson["id"], "title": lesson["text"],
            "detail": f"seen {lesson['occurrences']}×" if (lesson.get("occurrences") or 0) > 1 else "",
        })

    for po in sm.list_po_sessions(project_id):
        if po["status"] not in _PO_WAITING or po.get("publish_result"):
            continue
        draft = po.get("draft") or {}
        title = ((draft.get("bug") or draft.get("epic") or {}).get("summary")) or (po.get("brief") or "")[:120]
        items.append({
            "kind": PO_DRAFT, "project_id": project_id, "epic_key": po.get("epic_key"), "story_key": None, "key": None,
            "session_id": po["id"], "title": title,
            "detail": "needs your answers" if po["status"] == "awaiting_input" else "draft ready",
        })
    return items


def build_inbox(sm: StateManager | None = None, project_id: int | None = None) -> dict[str, Any]:
    """Every project (or just `project_id`) with its counts, and the items
    waiting on a person. A project id that does not exist yields no projects."""
    sm = sm or StateManager()
    projects = sm.list_projects()
    if project_id is not None:
        projects = [p for p in projects if p["id"] == project_id]
    wanted = {p["id"] for p in projects}

    epics_by_project: dict[int, list[dict]] = {pid: [] for pid in wanted}
    for epic_key in sm.list_all_epic_keys():
        epic = sm.get_epic(epic_key)
        if epic is not None and epic["project_id"] in wanted:
            epics_by_project[epic["project_id"]].append(epic)

    out_projects: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    for project in projects:
        pid = project["id"]
        items = _project_items(sm, pid, epics_by_project[pid])
        all_items.extend(items)
        out_projects.append({
            "id": pid,
            "name": project["name"],
            "counts": {
                "needs_you": len(items),
                "epics": len(epics_by_project[pid]),
                "repos": project.get("repo_count", 0),
                "lessons_proposed": sum(1 for i in items if i["kind"] == LESSON),
                "drafts": len(sm.list_po_sessions(pid)),
            },
        })
    return {"projects": out_projects, "items": all_items}
