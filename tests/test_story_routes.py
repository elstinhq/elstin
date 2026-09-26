"""PUT /api/stories/{key}/kind — the human override for forge's sync-time
guess at whether a ticket is implementation or validation work.

The endpoint's real job is not just flipping a column: reclassifying a
story changes when it must run, so it re-levels the epic in the same call.
A kind change that didn't move the wave would leave a QA ticket scheduled
to validate code that hasn't been written yet, which is the exact failure
the classification exists to prevent.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db", "fake_agents")


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


@pytest.fixture()
def epic(project_id) -> StateManager:
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2", "ENG-3"], ["ENG-4"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Build the form", kind="dev")
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Wire the API", kind="dev")
    sm.create_story("ENG-4", epic_key="ENG-1", wave=1, summary="Check it over", kind="dev")
    return sm


def test_reclassifying_to_qa_moves_the_story_to_a_trailing_wave(epic):
    with _client() as client:
        response = client.put("/api/stories/ENG-4/kind", json={"kind": "qa"})

    assert response.status_code == 200
    assert response.json()["levels"] == [["ENG-2", "ENG-3"], ["ENG-4"]]
    assert epic.get_story("ENG-4")["kind"] == "qa"
    assert epic.get_story("ENG-4")["kind_reason"] == "manual"


def test_reclassifying_pulls_the_story_out_of_a_shared_dev_wave(epic):
    with _client() as client:
        response = client.put("/api/stories/ENG-3/kind", json={"kind": "qa"})

    # ENG-3 leaves wave 0 and lands after every remaining dev story.
    assert response.json()["levels"] == [["ENG-2"], ["ENG-4"], ["ENG-3"]]
    assert epic.get_story("ENG-3")["wave"] == 2
    assert epic.get_story("ENG-2")["wave"] == 0


def test_reclassifying_an_in_flight_story_is_refused(epic):
    epic.set_story_status("ENG-2", "dispatched")

    with _client() as client:
        response = client.put("/api/stories/ENG-2/kind", json={"kind": "qa"})

    assert response.status_code == 409
    assert "already dispatched" in response.json()["detail"]
    assert epic.get_story("ENG-2")["kind"] == "dev"  # unchanged
    assert epic.get_epic("ENG-1")["levels"] == [["ENG-2", "ENG-3"], ["ENG-4"]]  # unchanged


def test_unknown_kind_is_rejected(epic):
    with _client() as client:
        response = client.put("/api/stories/ENG-2/kind", json={"kind": "reviewer"})
    assert response.status_code == 400


def test_unknown_story_is_a_404(epic):
    with _client() as client:
        response = client.put("/api/stories/ENG-999/kind", json={"kind": "qa"})
    assert response.status_code == 404


def test_get_story_exposes_kind_and_its_reason(epic):
    with _client() as client:
        story = client.get("/api/stories/ENG-2").json()
    assert story["kind"] == "dev"
    assert "kind_reason" in story


def test_get_story_exposes_run_outcome_once_dispatched(epic):
    from forge.services.usage import record_run_outcome

    with _client() as client:
        # Never dispatched: no thread_id yet, nothing to report.
        assert client.get("/api/stories/ENG-2").json()["run_outcome"] is None

    epic.set_story_thread_id("ENG-2", "ENG-2")
    record_run_outcome("ENG-2", dev_passes=4, gate_runs=4, gate_failures=3, verifier_runs=1, outcome="failed")

    with _client() as client:
        run_outcome = client.get("/api/stories/ENG-2").json()["run_outcome"]
    assert run_outcome["dev_passes"] == 4
    assert run_outcome["gate_failures"] == 3
    assert run_outcome["outcome"] == "failed"


def test_retry_story_redispatches_a_run_failure(epic):
    epic.set_epic_phase("ENG-1", "failed")
    epic.set_story_status("ENG-2", "failed", failure_stage="run")

    with _client() as client:
        response = client.post("/api/stories/ENG-2/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["retry_count"] == 1
    assert epic.get_epic("ENG-1")["phase"] == "implementing"


def test_retry_story_reattempts_a_merge_failure(epic):
    epic.set_story_status("ENG-2", "failed", failure_stage="merge")

    with _client() as client:
        response = client.post("/api/stories/ENG-2/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready_to_merge"
    assert body["merge_attempts"] == 0


def test_retry_non_failed_story_is_refused(epic):
    epic.set_story_status("ENG-2", "dispatched")

    with _client() as client:
        response = client.post("/api/stories/ENG-2/retry")

    assert response.status_code == 409
    assert epic.get_story("ENG-2")["status"] == "dispatched"  # unchanged


def test_retry_unknown_story_is_a_404():
    with _client() as client:
        response = client.post("/api/stories/ENG-999/retry")
    assert response.status_code == 404


_QUESTIONS = [
    {
        "id": "q0", "question": "Which store should own the cart total?", "context": "seen in cart.ts",
        "options": [
            {"id": "A", "label": "Redux", "description": "the existing global store"},
            {"id": "B", "label": "Local component state", "description": None},
        ],
    },
]


def test_answering_a_clarification_question_resolves_it_and_clears_the_gate(epic):
    epic.set_story_status("ENG-2", "awaiting_plan_approval")
    epic.set_story_clarification_questions("ENG-2", _QUESTIONS)

    with _client() as client:
        response = client.post("/api/stories/ENG-2/answer", json={"answers": [{"question_id": "q0", "option_id": "A"}]})

    assert response.status_code == 200
    body = response.json()
    assert body["clarification_questions"] == []
    assert body["clarification_answers"] == [
        {"question": "Which store should own the cart total?", "option_label": "Redux", "option_description": "the existing global store"},
    ]
    assert body["gate_approved"] is True


def test_answering_with_no_pending_questions_is_refused(epic):
    with _client() as client:
        response = client.post("/api/stories/ENG-2/answer", json={"answers": [{"question_id": "q0", "option_id": "A"}]})

    assert response.status_code == 409


def test_answering_leaves_a_missing_question_unanswered_as_a_422(epic):
    epic.set_story_clarification_questions("ENG-2", _QUESTIONS)

    with _client() as client:
        response = client.post("/api/stories/ENG-2/answer", json={"answers": []})

    assert response.status_code == 422
    assert "q0" in response.json()["detail"]
    # Nothing committed on a rejected, partial submission.
    assert epic.get_story("ENG-2")["clarification_questions"] == _QUESTIONS


def test_answering_with_an_option_the_question_never_offered_is_a_422(epic):
    epic.set_story_clarification_questions("ENG-2", _QUESTIONS)

    with _client() as client:
        response = client.post("/api/stories/ENG-2/answer", json={"answers": [{"question_id": "q0", "option_id": "Z"}]})

    assert response.status_code == 422
    assert epic.get_story("ENG-2")["clarification_questions"] == _QUESTIONS


def test_answer_unknown_story_is_a_404():
    with _client() as client:
        response = client.post("/api/stories/ENG-999/answer", json={"answers": []})
    assert response.status_code == 404


def test_sync_story_refreshes_prose_and_preserves_progress(epic, monkeypatch: pytest.MonkeyPatch):
    import forge.api.run_routes as run_routes

    epic.set_story_status("ENG-2", "dispatched")
    epic.update_story_jira_fields("ENG-2", summary="Stale", description="Stale body")

    def fake_sync(story_key: str) -> dict:
        epic.update_story_jira_fields(story_key, summary="Fresh summary", description="Fresh body")
        return epic.get_story(story_key)

    monkeypatch.setattr(run_routes, "sync_story_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/stories/ENG-2/sync")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "Fresh summary"
    assert body["description"] == "Fresh body"
    assert body["status"] == "dispatched"
    assert body["kind"] == "dev"
    assert "pull_request" in body


def test_sync_story_404_for_unknown_key(monkeypatch: pytest.MonkeyPatch):
    import forge.api.run_routes as run_routes

    def fake_sync(story_key: str) -> dict:
        raise ValueError(f"No such story: {story_key}")

    monkeypatch.setattr(run_routes, "sync_story_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/stories/ENG-999/sync")
    assert response.status_code == 404


def test_sync_story_404_when_jira_ticket_missing(epic, monkeypatch: pytest.MonkeyPatch):
    import forge.api.run_routes as run_routes
    from jira import JIRAError

    def fake_sync(story_key: str) -> dict:
        raise JIRAError("no such issue", status_code=404)

    monkeypatch.setattr(run_routes, "sync_story_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/stories/ENG-2/sync")
    assert response.status_code == 404


# --- POST /api/stories/{key}/start — the manual-run-mode Start button, and
# the immediate approve/retry follow-through it needs once a project turns
# auto_run_enabled off (forge/config/store.py). ----------------------------


@pytest.fixture()
def manual_run_epic(project_id, git_repo, tmp_path) -> StateManager:
    """A one-story, one-level epic under a project with auto_run_enabled
    off and plan approval off too, wired to a real repo — nothing
    dispatches it except an explicit POST .../start."""
    from forge.config.store import set_config, set_project_config

    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_project_config(project_id, "auto_run_enabled", "false")
    set_project_config(project_id, "require_plan_approval", "false")

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    repo_id = sm.add_project_repo(project_id, git_repo)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")
    return sm


def test_start_story_dispatches_a_pending_story(manual_run_epic):
    with _client() as client:
        response = client.post("/api/stories/ENG-2/start")

    assert response.status_code == 200
    assert response.json()["triggered"] == "DispatchStory"
    # Plan approval is off, so the run goes straight through to merge-ready.
    assert manual_run_epic.get_story("ENG-2")["status"] == "ready_to_merge"


def test_start_story_blocked_when_epic_on_hold(epic):
    """The `epic` fixture (unlike `manual_run_epic`) leaves manual_gate at
    its create_epic default of True."""
    with _client() as client:
        response = client.post("/api/stories/ENG-2/start")

    assert response.status_code == 409
    assert "epic_on_hold" in response.json()["detail"]
    assert epic.get_story("ENG-2")["status"] == "pending"  # untouched


def test_start_story_blocked_for_a_later_unresolved_level(manual_run_epic):
    manual_run_epic.set_epic_levels("ENG-1", [["ENG-2"], ["ENG-3"]])
    manual_run_epic.create_story("ENG-3", epic_key="ENG-1", wave=1, summary="Wire it up")

    with _client() as client:
        response = client.post("/api/stories/ENG-3/start")

    assert response.status_code == 409
    assert "waiting_on_dependencies" in response.json()["detail"]


def test_start_unknown_story_is_a_404():
    with _client() as client:
        response = client.post("/api/stories/ENG-999/start")
    assert response.status_code == 404


def test_approve_drives_a_manual_run_story_to_the_next_gate(manual_run_epic, monkeypatch: pytest.MonkeyPatch):
    """With plan approval off, dispatch alone already reaches ready_to_merge
    (see test_start_story_dispatches_a_pending_story), so this exercises the
    approve-triggers-immediately path by turning the plan-approval gate back
    on for just this scenario."""
    from forge.config.store import set_project_config

    project_id = manual_run_epic.get_epic("ENG-1")["project_id"]
    set_project_config(project_id, "require_plan_approval", "true")

    with _client() as client:
        start_response = client.post("/api/stories/ENG-2/start")
        assert start_response.status_code == 200
        assert manual_run_epic.get_story("ENG-2")["status"] == "awaiting_plan_approval"

        approve_response = client.post("/api/stories/ENG-2/approve")

    assert approve_response.status_code == 200
    # No poll loop runs for this project — approve had to drive the resume
    # itself, landing on ready_to_merge.
    assert manual_run_epic.get_story("ENG-2")["status"] == "ready_to_merge"
    assert manual_run_epic.get_story("ENG-2")["gate_approved"] is False


def test_retry_still_leaves_an_auto_run_project_story_pending(epic):
    """Unchanged behavior for the default (auto_run_enabled=true) project:
    retry resets state but does not itself dispatch — the existing 15s poll
    loop covers it, exactly as before this feature."""
    epic.set_epic_phase("ENG-1", "failed")
    epic.set_story_status("ENG-2", "failed", failure_stage="run")

    with _client() as client:
        response = client.post("/api/stories/ENG-2/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
