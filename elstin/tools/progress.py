"""The dev agent's live progress checklist.

An approved plan is static, so on a long run the model's only record of
what it set out to do is buried dozens of turns back in a history that
compaction has since tombstoned. This is the live counterpart: seeded from
the plan's file list, updated by the model through
elstin/tools/dev_tools.py's `update_progress_tool`, and re-shown at the end
of every request via elstin/agents/loop.py's `trailing_reminder`. The
re-showing is the point — it is what turns the idle-turn budget from a
silent stop into something the model can correct before it trips.

Deliberately files-only rather than free-form tasks: the plan's file list
is the one part of the scope the tool layer already enforces
(elstin/tools/path_guard.py), so a checklist keyed on it can never drift
into work the agent is not allowed to do.

Lives under tools/ rather than beside the dev prompt because both the tool
layer and the role need it, and `tools/` importing from `agents/roles/`
would invert the dependency direction the package map sets out.
"""
from __future__ import annotations

import os
from typing import Any

PROGRESS_STATUSES = ("pending", "doing", "done")

_STATUS_MARKS = {"pending": " ", "doing": ">", "done": "x"}


def _norm_rel(path: str) -> str:
    return os.path.normpath(str(path).strip().lstrip("/")).replace("\\", "/")


def seed_progress_items(plan: dict[str, Any]) -> list[dict[str, str]]:
    """One checklist entry per planned file, in plan order."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, verb in (("files_to_create", "create"), ("files_to_modify", "modify")):
        for entry in plan.get(key) or []:
            rel = entry.get("path") if isinstance(entry, dict) else str(entry)
            if not rel:
                continue
            path = _norm_rel(rel)
            if path in seen:
                continue
            seen.add(path)
            items.append({"path": path, "action": verb, "status": "pending"})
    return items


def render_progress(items: list[dict[str, str]]) -> str:
    """The trailing reminder text. Empty when there is nothing to track,
    so a plan with no file list costs nothing per turn."""
    if not items:
        return ""
    lines = [
        f"- [{_STATUS_MARKS.get(item.get('status', 'pending'), ' ')}] {item['action']} {item['path']}"
        for item in items
    ]
    done = sum(1 for item in items if item.get("status") == "done")
    remaining = [item["path"] for item in items if item.get("status") != "done"]
    body = "\n".join(lines)
    footer = (
        f"{done}/{len(items)} done."
        + (f" Still to do: {', '.join(remaining[:6])}." if remaining else " Every planned file is marked done — verify with analyze_changed_files_tool, then summarize and stop.")
    )
    return (
        "PROGRESS CHECKLIST (yours to maintain — call update_progress_tool whenever a file's state changes):\n"
        + body
        + "\n"
        + footer
    )


def merge_progress_update(items: list[dict[str, str]], updates: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Apply the model's status updates in place. Returns (applied_count,
    rejected_paths).

    Unknown paths are rejected rather than added: the checklist mirrors the
    approved plan, and letting the agent invent entries would turn its own
    bookkeeping into a second, unenforced scope."""
    by_path = {item["path"]: item for item in items}
    applied = 0
    rejected: list[str] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        path = _norm_rel(str(update.get("path") or ""))
        status = str(update.get("status") or "").strip().lower()
        target = by_path.get(path)
        if target is None or status not in PROGRESS_STATUSES:
            rejected.append(path or "(no path)")
            continue
        target["status"] = status
        applied += 1
    return applied, rejected
