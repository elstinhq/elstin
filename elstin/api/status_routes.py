"""What this process is working on right now — read only.

The Mac app's window asks it (every few seconds) for two things: whether quitting
would stop something — it says so before it does — and whether the Mac should be kept
awake, because a laptop that sleeps pauses every agent. In-memory, like the sets it
reads: a restart ends them all, and the reconciler resumes what they leave behind
(elstin/daemon/main.py's `_live_runs`)."""
from __future__ import annotations

from fastapi import APIRouter

from elstin.daemon.main import live_run_keys
from elstin.daemon.po_runner import live_po_session_ids, live_publish_session_ids

router = APIRouter(tags=["status"])


@router.get("/api/status")
def get_status() -> dict:
    stories = sorted(live_run_keys())  # a story's run, or a merge into its epic
    po_sessions = len(live_po_session_ids())
    publishing = len(live_publish_session_ids())
    return {
        "busy": bool(stories or po_sessions or publishing),
        "stories": stories,
        "po_sessions": po_sessions,
        "publishing": publishing,
    }
