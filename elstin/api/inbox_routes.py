"""What is waiting on a person ("Needs you"), across projects or for one.
Read only — see elstin/services/inbox.py. Each item's action goes through the
route that owns it (approve, answer, pr-approval, lessons, PO sessions)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from elstin.services.inbox import build_inbox
from elstin.state.manager import StateManager

router = APIRouter(tags=["inbox"])


@router.get("/api/inbox")
def get_inbox() -> dict:
    return build_inbox(StateManager())


@router.get("/api/projects/{project_id}/inbox")
def get_project_inbox(project_id: int) -> dict:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return build_inbox(sm, project_id)
