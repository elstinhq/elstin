"""What a plan leaves to a person that is not a file (plan_gate.person_actions_of):
kept on the story, listed in the epic's pull request, and replaced by the next
plan. The file-shaped counterpart is read_only_changes, covered in
tests/test_reference_folders.py; this follows it end to end."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_actions_a_plan_leaves_to_a_person_are_kept_and_named_in_the_epic_pull_request(git_repo, tmp_path, project_id, monkeypatch):
    """Reconciling against an extract the agent cannot reach is a
    person's to run. The plan goes ahead without it; the board shows it, and the
    epic's pull request says it is not done until someone does it."""
    import elstin.daemon.main as daemon
    import elstin.orchestration.graph as graph
    from elstin.config.store import set_config
    from elstin.state.manager import StateManager
    from tests.conftest import fake_run_planner

    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "true")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False, summary="Item search")
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="reconcile the grid", depends_on=[])
    handed = [
        {"action": "Run the script against the reference extract and record the nine rows", "reason": "the agent has no access to the extract"},
        {"action": "Publish the delta sheet", "reason": None},
    ]
    plans = [handed, []]

    async def planner(**kwargs):
        result = await fake_run_planner(**kwargs)
        result["plan"]["person_actions"] = plans.pop(0)
        return result

    monkeypatch.setattr(graph, "run_planner", planner)

    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-2")
    assert story["status"] == "awaiting_plan_approval" and story["person_actions"] == handed
    body = daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))
    assert "## Left to a person" in body
    assert "- Run the script against the reference extract and record the nine rows (ENG-2) — the agent has no access to the extract" in body
    assert "- Publish the delta sheet (ENG-2)" in body

    assert sm.send_back_to_planning("ENG-2")  # as a changed ticket or newly merged work does
    await daemon.poll_once(sm, wait=True)

    assert sm.get_story("ENG-2")["person_actions"] == []
    assert "Left to a person" not in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))


@pytest.mark.usefixtures("elstin_db")
def test_a_story_with_no_person_actions_reads_back_an_empty_list(project_id):
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=True, summary="x")
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="y", depends_on=[])

    assert sm.get_story("ENG-2")["person_actions"] == []

    sm.set_story_person_actions("ENG-2", [{"action": "Publish it", "reason": None}])
    assert sm.get_story("ENG-2")["person_actions"] == [{"action": "Publish it", "reason": None}]

    sm.set_story_person_actions("ENG-2", None)
    assert sm.get_story("ENG-2")["person_actions"] == []
