"""End-to-end for the QA path, against real Postgres and a real git repo:
a dev story runs and merges, then the epic's QA ticket dispatches in the
final wave, gets a QA context assembled from the merged integration branch,
completes without a PR, and has its report posted to JIRA.

Only the model call inside forge/agents/roles/qa.py is faked (via the
`fake_agents` fixture) — the wave sequencing, worktree creation off the
integration branch, context assembly and status mapping are all real.
"""
from __future__ import annotations

import os

import pytest

import forge.daemon.main as daemon
from forge.config.store import set_config
from forge.daemon.main import poll_once
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


def _configure(worktrees_base_dir: str) -> None:
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", worktrees_base_dir)


@pytest.fixture()
def captured_jira_comments(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Stands in for JiraService so the QA report's delivery is observable
    without a real JIRA. `_post_qa_report` is the report's only consumer, so
    if this never fires the stage produced nothing anyone reads."""
    posted: list[tuple[str, str]] = []

    class _FakeJira:
        def __init__(self, project_id=None) -> None:
            self.project_id = project_id

        @staticmethod
        def is_configured(project_id=None) -> bool:
            return True

        def add_comment(self, issue_key: str, body: str) -> bool:
            posted.append((issue_key, body))
            return True

    monkeypatch.setattr(daemon, "JiraService", _FakeJira)
    return posted


async def _drive_to_completion(sm: StateManager, story_key: str) -> None:
    """Walks one dev story through all three of its gates."""
    await poll_once(sm)  # dispatch -> plan-approval gate
    sm.set_story_gate_approved(story_key)
    await poll_once(sm)  # resume -> PR-open gate
    sm.set_story_gate_approved(story_key)
    await poll_once(sm)  # open PR -> PR-review gate
    sm.set_story_gate_approved(story_key)
    await poll_once(sm)  # merge into the integration branch


async def test_qa_story_runs_last_and_reports_without_opening_a_pr(
    git_repo: str, tmp_path, project_id, captured_jira_comments, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"], ["ENG-9"]])
    repo_id = sm.add_project_repo(project_id, git_repo)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", kind="dev")
    sm.create_story("ENG-9", epic_key="ENG-1", wave=1, summary="Validate the end-to-end flow", kind="qa")

    # The QA ticket must not move while the dev wave is still in flight.
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"
    assert sm.get_story("ENG-9")["status"] == "pending"

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)  # resume -> PR-open gate
    assert sm.get_story("ENG-9")["status"] == "pending"  # still gated behind wave 0

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)  # open PR -> PR-review gate
    assert sm.get_story("ENG-9")["status"] == "pending"  # still gated behind wave 0

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "merged"

    # --- wave 1: the QA ticket ---
    await poll_once(sm)
    qa_story = sm.get_story("ENG-9")

    # Terminal-ok, not awaiting_pr_review: a QA story produces a report, so
    # there is no branch to review and no PR to open.
    assert qa_story["status"] == "done"
    assert sm.get_pull_request("ENG-9") is None

    # The report reached the QA ticket, which is the whole point of the stage.
    assert [key for key, _body in captured_jira_comments] == ["ENG-9"]
    assert "Automated epic QA — ENG-1" in captured_jira_comments[0][1]

    # And the epic is now complete.
    await poll_once(sm)
    assert sm.get_epic("ENG-1")["phase"] == "complete"


async def test_qa_context_carries_the_epic_and_its_dev_stories(
    git_repo: str, tmp_path, project_id, monkeypatch: pytest.MonkeyPatch
):
    """The QA review is only as good as what it is handed. This pins that
    the daemon assembles the epic's implementation stories and the merged
    branch diff — a review run without them would be reviewing nothing."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"], ["ENG-9"]])
    repo_id = sm.add_project_repo(project_id, git_repo)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget",
                    description="AC1: the widget exists.", kind="dev")
    sm.create_story("ENG-9", epic_key="ENG-1", wave=1, summary="Validate the flow", kind="qa")

    await _drive_to_completion(sm, "ENG-2")

    epic = sm.get_epic("ENG-1")
    context = daemon._build_qa_context(sm, epic, sm.get_story("ENG-9"))

    assert context["qa_story"]["jira_key"] == "ENG-9"
    assert context["epic"]["jira_key"] == "ENG-1"
    # Only implementation stories — QA does not review itself.
    assert [s["jira_key"] for s in context["dev_stories"]] == ["ENG-2"]
    assert context["dev_stories"][0]["description"] == "AC1: the widget exists."
    assert context["branch_diff"]["branch"] is not None


async def test_qa_context_without_an_integration_branch_reports_an_empty_diff(
    git_repo: str, tmp_path, project_id, monkeypatch: pytest.MonkeyPatch
):
    """An epic where nothing merged has no integration branch. The context
    must still be well-formed rather than raising — but it must not invent
    a diff either."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-9"]])
    repo_id = sm.add_project_repo(project_id, git_repo)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-9", epic_key="ENG-1", wave=0, summary="Validate the flow", kind="qa")

    context = daemon._build_qa_context(sm, sm.get_epic("ENG-1"), sm.get_story("ENG-9"))

    assert context["branch_diff"]["branch"] is None
    assert context["branch_diff"]["changed_files"] == []
    assert context["dev_stories"] == []


async def test_story_brief_folds_in_its_subtasks(git_repo: str, tmp_path, project_id, monkeypatch):
    """Sub-tasks aren't dispatched, so their file-level detail only reaches
    the agent if the parent's brief carries it. If this regresses, forge
    silently plans five stories with none of the breakdown the team wrote."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Select multiple item types",
                    description="AC1: I can add another item type.", kind="dev")
    sm.create_story("ENG-20", epic_key="ENG-1", wave=0, parent_key="ENG-2", status="skipped",
                    summary="[New] SelectedItemLine model",
                    description="Create lib/features/cart_module/model/selected_item_line.dart")
    sm.create_story("ENG-21", epic_key="ENG-1", wave=0, parent_key="ENG-2", status="skipped",
                    summary="[Extend] SearchParameters for item lines list")

    brief = daemon._build_story_brief(sm, sm.get_story("ENG-2"))

    assert brief.startswith("Select multiple item types")
    assert "AC1: I can add another item type." in brief
    assert "Implementation breakdown (2 JIRA sub-tasks)" in brief
    assert "ENG-20 — [New] SelectedItemLine model" in brief
    assert "selected_item_line.dart" in brief
    assert "ENG-21 — [Extend] SearchParameters for item lines list" in brief
    # And it says they ship together, so the agent doesn't defer them.
    assert "delivered together with it" in brief


async def test_story_brief_without_subtasks_is_just_the_ticket(git_repo: str, tmp_path, project_id, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", description="Do it well.")

    brief = daemon._build_story_brief(sm, sm.get_story("ENG-2"))
    assert brief == "Add a widget\n\nDo it well."


async def test_story_brief_appends_a_codebase_skeleton_when_given_the_repo(git_repo: str, tmp_path, project_id, monkeypatch):
    """The planner's brief carries a pre-computed map of the repo so the
    model starts at the right files instead of exploring the tree."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))
    os.makedirs(os.path.join(git_repo, "app"), exist_ok=True)
    with open(os.path.join(git_repo, "app", "service.py"), "w") as f:
        f.write("class WidgetService:\n    pass\n")

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", description="Do it well.")

    brief = daemon._build_story_brief(sm, sm.get_story("ENG-2"), git_repo)

    assert brief.startswith("Add a widget\n\nDo it well.\n\n## Codebase skeleton (")
    assert "app/: service.py [WidgetService]" in brief


async def test_subtasks_are_never_dispatched(git_repo: str, tmp_path, project_id, monkeypatch):
    """End-to-end guard: a sub-task row present in the DB must not get a
    worktree, a branch, or a status change when the daemon polls."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    repo_id = sm.add_project_repo(project_id, git_repo)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Build the form")
    sm.create_story("ENG-20", epic_key="ENG-1", wave=0, parent_key="ENG-2", status="skipped",
                    summary="[New] SelectedItemLine model")

    await poll_once(sm)

    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"
    subtask = sm.get_story("ENG-20")
    assert subtask["status"] == "skipped"
    assert subtask["worktree_path"] is None
    assert subtask["branch"] is None
    assert subtask["thread_id"] is None


async def test_qa_context_nests_subtasks_under_their_story(git_repo: str, tmp_path, project_id, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"], ["ENG-9"]])
    repo_id = sm.add_project_repo(project_id, git_repo)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Build the form", kind="dev")
    sm.create_story("ENG-20", epic_key="ENG-1", wave=0, parent_key="ENG-2", status="skipped",
                    summary="[New] SelectedItemLine model", kind="dev")
    sm.create_story("ENG-9", epic_key="ENG-1", wave=1, summary="Validate the flow", kind="qa")

    context = daemon._build_qa_context(sm, sm.get_epic("ENG-1"), sm.get_story("ENG-9"))

    # One deliverable, not two — QA must see the epic the way the team scoped it.
    assert [s["jira_key"] for s in context["dev_stories"]] == ["ENG-2"]
    assert [t["jira_key"] for t in context["dev_stories"][0]["subtasks"]] == ["ENG-20"]


# --- multi-repo workspace (forge/tools/workspace.py) -------------------


def _add_reference_repo(sm, project_id: int, tmp_path, name: str, rel: str, content: str) -> str:
    """A sibling repo on the same project. Only ever read, so it needs no
    git history — just files on disk."""
    root = tmp_path / name
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    sm.add_project_repo(project_id, str(root), label=name)
    return str(root)


async def test_reference_roots_are_every_project_repo_except_the_storys_own(
    git_repo: str, tmp_path, project_id, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    repo_id = sm.add_project_repo(project_id, git_repo, label="web-ui")
    sm.set_epic_project_repo("ENG-1", repo_id)
    api = _add_reference_repo(sm, project_id, tmp_path, "api", "models/cart.py", "class Cart:\n    pass\n")

    epic = sm.get_epic("ENG-1")

    # A story writing to the epic's default repo reads the API repo.
    assert daemon._reference_roots(sm, epic, git_repo) == {"api": api}
    # And a story overridden onto the API repo reads the UI one instead —
    # the writable repo is never among its own references.
    assert daemon._reference_roots(sm, epic, api) == {"web-ui": git_repo}


async def test_story_brief_maps_the_reference_repo_with_its_prefix(
    git_repo: str, tmp_path, project_id, monkeypatch
):
    """The skeleton doubles as documentation of how to address the other
    repo, so the label has to appear with the prefix its paths need."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Show the cart total")
    api = _add_reference_repo(sm, project_id, tmp_path, "api", "models/cart.py", "class Cart:\n    pass\n")

    brief = daemon._build_story_brief(sm, sm.get_story("ENG-2"), git_repo, {"api": api})

    assert "### Reference repo @api (read-only)" in brief
    assert "Address these files as @api/<path>" in brief
    assert "models/: cart.py [Cart]" in brief


async def test_dispatch_hands_the_reference_roots_to_the_planner(
    git_repo: str, tmp_path, project_id, monkeypatch
):
    """End-to-end plumbing: daemon assembles the roots, the graph state
    carries them, and the role receives them."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    repo_id = sm.add_project_repo(project_id, git_repo, label="web-ui")
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Show the cart total")
    api = _add_reference_repo(sm, project_id, tmp_path, "api", "models/cart.py", "class Cart:\n    pass\n")

    import forge.orchestration.graph as graph_module
    from tests.conftest import fake_run_planner

    seen: dict = {}

    async def capturing_planner(**kwargs):
        seen.update(kwargs)
        return await fake_run_planner(**kwargs)

    monkeypatch.setattr(graph_module, "run_planner", capturing_planner)

    await poll_once(sm)

    assert seen["reference_roots"] == {"api": api}
