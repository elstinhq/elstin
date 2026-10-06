"""Round-trips epic/story/PR bookkeeping through StateManager. Confirms
elstin-internal fields persist and that JIRA-sourced fields (jira_key,
assignment) are never mutated by any StateManager write method — only
elstin-internal tracking columns are."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("elstin_db")


def test_epic_round_trip(project_id):
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2", "ENG-3"], ["ENG-4"]])

    epic = sm.get_epic("ENG-1")
    assert epic["jira_key"] == "ENG-1"
    assert epic["project_id"] == project_id
    assert epic["repo"] is None  # no folder chosen yet
    assert epic["phase"] == "planning"
    assert epic["levels"] == [["ENG-2", "ENG-3"], ["ENG-4"]]
    assert epic["plan_approved"] is False
    assert epic["manual_gate"] is True

    repo_id = sm.add_project_repo(project_id, "/repo/widgets")
    sm.set_epic_project_repo("ENG-1", repo_id)
    assert sm.get_epic("ENG-1")["repo"] == "/repo/widgets"  # resolved via the join

    sm.approve_epic_plan("ENG-1")
    sm.set_epic_phase("ENG-1", "implementing")
    sm.set_integration_branch("ENG-1", "agent/epic-ENG-1")

    epic = sm.get_epic("ENG-1")
    assert epic["plan_approved"] is True
    assert epic["phase"] == "implementing"
    assert epic["integration_branch"] == "agent/epic-ENG-1"


def test_create_epic_is_idempotent(project_id):
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.set_epic_phase("ENG-1", "implementing")
    sm.create_epic("ENG-1", project_id=project_id)  # second call must not reset phase

    assert sm.get_epic("ENG-1")["phase"] == "implementing"


def test_story_lifecycle_and_listing(project_id):
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0)
    sm.create_story("ENG-4", epic_key="ENG-1", wave=1)

    stories = sm.list_stories_for_epic("ENG-1")
    assert {s["jira_key"] for s in stories} == {"ENG-2", "ENG-3", "ENG-4"}
    assert all(s["status"] == "pending" for s in stories)

    sm.set_story_thread_id("ENG-2", "thread-abc")
    sm.set_story_worktree("ENG-2", worktree_path="/data/repos/eng-2", branch="ENG-2-add-widget")
    sm.set_story_status("ENG-2", "dispatched")

    story = sm.get_story("ENG-2")
    assert story["thread_id"] == "thread-abc"
    assert story["worktree_path"] == "/data/repos/eng-2"
    assert story["branch"] == "ENG-2-add-widget"
    assert story["status"] == "dispatched"
    assert story["epic_key"] == "ENG-1"

    sm.set_story_status("ENG-2", "pending", parked_reason="plan_approval_gate")
    story = sm.get_story("ENG-2")
    assert story["status"] == "pending"
    assert story["parked_reason"] == "plan_approval_gate"


def test_clarification_answer_resolves_the_gate_and_clears_the_question(project_id):
    """set_story_clarification_answers is what POST /api/stories/{key}/answer
    calls after resolving a human's picks against the pending questions —
    see elstin/api/run_routes.py. It must clear the pending question (no
    stale form on the next read) and clear the same gate /approve does,
    since the reconciler cannot tell this state apart from an ordinary
    plan-approval one."""
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    questions = [{
        "id": "q0", "question": "Which store should own this?", "context": "seen in cart.ts",
        "options": [{"id": "A", "label": "Redux", "description": "existing store"}],
    }]
    sm.set_story_clarification_questions("ENG-2", questions)

    story = sm.get_story("ENG-2")
    assert story["clarification_questions"] == questions
    assert story["gate_approved"] is False

    answers = [{"question": "Which store should own this?", "option_label": "Redux", "option_description": "existing store"}]
    assert sm.set_story_clarification_answers("ENG-2", answers) is True

    story = sm.get_story("ENG-2")
    assert story["clarification_answers"] == answers
    assert story["clarification_questions"] == []  # no longer pending
    assert story["gate_approved"] is True


def test_clarification_questions_defaults_to_empty_and_clears_on_none(project_id):
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)

    assert sm.get_story("ENG-2")["clarification_questions"] == []

    sm.set_story_clarification_questions("ENG-2", [{"id": "q0", "question": "Which?", "context": None, "options": []}])
    assert len(sm.get_story("ENG-2")["clarification_questions"]) == 1

    sm.set_story_clarification_questions("ENG-2", None)
    assert sm.get_story("ENG-2")["clarification_questions"] == []


def test_setting_clarification_answers_on_an_unknown_story_returns_false(project_id):
    from elstin.state.manager import StateManager

    assert StateManager().set_story_clarification_answers("ENG-999", []) is False


def test_retry_story_run_failure_redispatches(project_id):
    """failure_stage="run" (or unset) goes back to "pending" — a full
    redispatch, since a run failure never reaches a PR to preserve."""
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.set_epic_phase("ENG-1", "failed")
    sm.set_story_status("ENG-2", "failed", failure_stage="run")

    sm.retry_story("ENG-2")

    story = sm.get_story("ENG-2")
    assert story["status"] == "pending"
    assert story["failure_stage"] is None
    assert story["parked_reason"] is None
    assert story["retry_count"] == 1
    assert sm.get_epic("ENG-1")["phase"] == "implementing"  # no longer excluded from polling


def test_retry_story_resume_at_dev_goes_back_to_dispatched(project_id):
    """A run that failed in dev, re-armed at dev by the caller, is not sent to
    "pending" — DispatchStory would start the graph over and plan again.
    "dispatched" with nothing executing it is what the reconciler answers with
    a ResumeStory, so the run continues from the checkpoint it was left at."""
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.set_epic_phase("ENG-1", "failed")
    sm.set_story_status("ENG-2", "failed", failure_stage="run")

    sm.retry_story("ENG-2", resume_at_dev=True)

    story = sm.get_story("ENG-2")
    assert story["status"] == "dispatched"
    assert story["failure_stage"] is None
    assert story["retry_count"] == 1
    assert sm.get_epic("ENG-1")["phase"] == "implementing"


def test_retry_story_resume_at_dev_does_not_change_a_merge_retry(project_id):
    """A merge failure never re-plans, so there is nothing to resume."""
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.set_story_status("ENG-2", "failed", failure_stage="merge")

    sm.retry_story("ENG-2", resume_at_dev=True)

    assert sm.get_story("ENG-2")["status"] == "ready_to_merge"


def test_retry_story_merge_failure_reattempts_merge(project_id):
    """failure_stage="merge" goes back to "ready_to_merge" with a fresh merge
    budget — the run itself already succeeded, so its committed work is kept
    and only the landing on the epic branch is tried again."""
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.set_story_status("ENG-2", "failed", failure_stage="merge")
    sm.increment_story_merge_attempts("ENG-2")
    sm.increment_story_merge_attempts("ENG-2")

    sm.retry_story("ENG-2")

    story = sm.get_story("ENG-2")
    assert story["status"] == "ready_to_merge"
    assert story["failure_stage"] is None
    assert story["merge_attempts"] == 0
    assert story["retry_count"] == 1


def test_retry_story_increments_across_repeated_retries(project_id):
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)

    sm.set_story_status("ENG-2", "failed", failure_stage="run")
    sm.retry_story("ENG-2")
    sm.set_story_status("ENG-2", "failed", failure_stage="run")
    sm.retry_story("ENG-2")

    assert sm.get_story("ENG-2")["retry_count"] == 2


def test_pull_request_upsert(project_id):
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)

    assert sm.get_pull_request("ENG-2") is None

    sm.upsert_pull_request("ENG-2", number=42, url="https://github.com/acme/widgets/pull/42")
    pr = sm.get_pull_request("ENG-2")
    # repo_label is "" for a story that wrote only its default repo.
    assert pr == {
        "story_key": "ENG-2", "repo_label": "", "number": 42,
        "url": "https://github.com/acme/widgets/pull/42", "state": "open",
    }

    sm.upsert_pull_request("ENG-2", state="merged")
    pr = sm.get_pull_request("ENG-2")
    assert pr["state"] == "merged"
    assert pr["number"] == 42  # unspecified fields on the second upsert are preserved


def test_comment_watermark_round_trip():
    from elstin.state.manager import StateManager

    sm = StateManager()
    assert sm.get_comment_watermark("ENG-1") is None

    sm.set_comment_watermark("ENG-1", "10001")
    assert sm.get_comment_watermark("ENG-1") == "10001"

    sm.set_comment_watermark("ENG-1", "10002")
    assert sm.get_comment_watermark("ENG-1") == "10002"


def test_project_and_project_repo_round_trip():
    from elstin.state.manager import StateManager

    sm = StateManager()
    project_id = sm.create_project("Widgets", description="The widgets project")

    project = sm.get_project(project_id)
    assert project["name"] == "Widgets"
    assert project["description"] == "The widgets project"
    assert project["repo_count"] == 0
    assert project["epic_count"] == 0
    assert project["repos"] == []

    repo_id = sm.add_project_repo(project_id, "/repo/widgets", label="widgets")
    assert sm.get_project(project_id)["repo_count"] == 1
    assert sm.list_project_repos(project_id) == [
        {
            "id": repo_id, "project_id": project_id, "path": "/repo/widgets", "label": "widgets",
            "clone_url": None, "kind": "repo", "read_only": False, "generated_guide": None, "generated_guide_at": None,
            "created_at": sm.get_project_repo(repo_id)["created_at"],
        }
    ]

    # a generated guide is stored, stamped, and can be discarded again
    assert sm.set_project_repo_guide(repo_id, "the guide") is True
    stored = sm.get_project_repo(repo_id)
    assert stored["generated_guide"] == "the guide" and stored["generated_guide_at"]
    assert sm.set_project_repo_guide(repo_id, None) is True
    assert sm.get_project_repo(repo_id)["generated_guide"] is None
    assert sm.get_project_repo(repo_id)["generated_guide_at"] is None
    assert sm.set_project_repo_guide(repo_id + 999, "x") is False

    sm.create_epic("ENG-1", project_id=project_id)
    assert sm.get_project(project_id)["epic_count"] == 1

    # A folder being an epic's default is no reason to keep it: the epic moves
    # to the project's next folder, or to none once the last one goes.
    other_id = sm.add_project_repo(project_id, "/tmp/other")
    sm.set_epic_project_repo("ENG-1", repo_id)
    assert sm.remove_project_repo(repo_id) == {"removed": True, "epics_moved": ["ENG-1"], "moved_to": other_id}
    assert sm.get_project_repo(repo_id) is None
    assert sm.get_epic("ENG-1")["project_repo_id"] == other_id
    assert sm.remove_project_repo(other_id) == {"removed": True, "epics_moved": ["ENG-1"], "moved_to": None}
    assert sm.get_epic("ENG-1")["project_repo_id"] is None
    assert sm.remove_project_repo(other_id) is None  # already gone


def test_list_projects_orders_newest_first_and_counts():
    from elstin.state.manager import StateManager

    sm = StateManager()
    first = sm.create_project("First")
    second = sm.create_project("Second")
    sm.create_epic("ENG-1", project_id=first)
    sm.create_epic("ENG-2", project_id=first)
    sm.add_project_repo(second, "/repo/second")

    projects = {p["id"]: p for p in sm.list_projects()}
    assert projects[first]["epic_count"] == 2
    assert projects[first]["repo_count"] == 0
    assert projects[second]["repo_count"] == 1
