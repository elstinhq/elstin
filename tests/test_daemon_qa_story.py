"""End-to-end for the QA path, against real Postgres and a real git repo:
a dev story runs and merges, then the epic's QA ticket dispatches in the
final wave, gets a QA context assembled from the merged integration branch,
completes without a PR, and has its report posted to JIRA.

Only the model call inside elstin/agents/roles/qa.py is faked (via the
`fake_agents` fixture) — the wave sequencing, worktree creation off the
integration branch, context assembly and status mapping are all real.
"""
from __future__ import annotations

import os

import pytest

import elstin.daemon.main as daemon
from elstin.config.store import set_config
from elstin.daemon.main import poll_once
from elstin.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("elstin_db", "fake_agents"), pytest.mark.asyncio]


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

    monkeypatch.setattr(daemon, "get_ticket_source", lambda project_id: _FakeJira(project_id))
    monkeypatch.setattr(daemon, "ticket_source_configured", _FakeJira.is_configured)
    return posted


async def _drive_to_completion(sm: StateManager, story_key: str) -> None:
    """Walks one dev story through all three of its gates."""
    await poll_once(sm, wait=True)  # dispatch -> plan-approval gate
    sm.set_story_gate_approved(story_key)
    await poll_once(sm, wait=True)  # resume -> PR-open gate
    sm.set_story_gate_approved(story_key)
    await poll_once(sm, wait=True)  # open PR -> PR-review gate
    sm.set_story_gate_approved(story_key)
    await poll_once(sm, wait=True)  # merge into the integration branch


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
    await poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"
    assert sm.get_story("ENG-9")["status"] == "pending"

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm, wait=True)  # resume -> PR-open gate
    assert sm.get_story("ENG-9")["status"] == "pending"  # still gated behind wave 0

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm, wait=True)  # open PR -> PR-review gate
    assert sm.get_story("ENG-9")["status"] == "pending"  # still gated behind wave 0

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["status"] == "merged"

    # --- wave 1: the QA ticket ---
    await poll_once(sm, wait=True)
    qa_story = sm.get_story("ENG-9")

    # Terminal-ok, not ready_to_merge: a QA story produces a report, so
    # there is no branch to review and no PR to open.
    assert qa_story["status"] == "done"
    assert sm.get_pull_request("ENG-9") is None

    # The report reached the QA ticket, which is the whole point of the stage.
    assert [key for key, _body in captured_jira_comments] == ["ENG-9"]
    assert "Automated epic QA — ENG-1" in captured_jira_comments[0][1]

    # And the epic is now complete.
    await poll_once(sm, wait=True)
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
    the agent if the parent's brief carries it. If this regresses, elstin
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


async def test_story_brief_carries_the_epic_and_its_other_stories(git_repo: str, tmp_path, project_id, monkeypatch):
    """The planner of one story sees the whole epic: what it is for, and
    which sibling stories exist and have merged — so it neither guesses the
    epic's vocabulary nor plans a sibling's work."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2", "ENG-3"], ["ENG-4"]],
                   summary="Multi-item pricing", description="Buyers price several item types in one quote.")
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Price model", depends_on=[])
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Currency column", depends_on=[])
    sm.create_story("ENG-4", epic_key="ENG-1", wave=1, summary="Quote screen", description="Show the quote.",
                    depends_on=["ENG-2"])
    sm.set_story_status("ENG-2", "merged")

    brief = daemon._build_story_brief(sm, sm.get_story("ENG-4"))

    assert brief.startswith("Quote screen\n\nShow the quote.")  # the story itself leads
    assert "## Epic ENG-1: Multi-item pricing" in brief
    assert "Buyers price several item types in one quote." in brief
    assert "- ENG-2: Price model [merged into the epic branch; this story depends on it]" in brief
    assert "- ENG-3: Currency column [not merged yet]" in brief
    assert "implement only this story's own scope" in brief


async def test_a_lone_story_that_is_its_own_epic_gets_no_epic_section(git_repo: str, tmp_path, project_id, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-7", project_id=project_id, manual_gate=False, levels=[["ENG-7"]],
                   summary="Fix the header", description="It overlaps.")
    sm.create_story("ENG-7", epic_key="ENG-7", wave=0, summary="Fix the header", description="It overlaps.")

    assert daemon._build_story_brief(sm, sm.get_story("ENG-7")) == "Fix the header\n\nIt overlaps."


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

    await poll_once(sm, wait=True)

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


# --- multi-repo workspace (elstin/tools/workspace.py) -------------------


def _add_reference_repo(sm, project_id: int, tmp_path, name: str, rel: str, content: str) -> str:
    """A sibling repo on the same project. Only ever read, so it needs no
    git history — just files on disk."""
    root = tmp_path / name
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    sm.add_project_repo(project_id, str(root), label=name)
    return str(root)


async def test_workspace_roots_cover_every_repo_on_the_project(
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

    # Every repo is in the workspace; the default only fixes which one an
    # unprefixed path means.
    default_label, roots, read_only, reference = daemon._workspace_roots(sm, epic, git_repo)
    assert default_label == "web-ui"
    assert roots == {"web-ui": git_repo, "api": api}
    assert read_only == [] and reference == []

    other_default, other_roots, _, _ = daemon._workspace_roots(sm, epic, api)
    assert other_default == "api"
    assert other_roots == roots


async def test_read_only_repos_and_reference_folders_are_in_the_workspace_but_marked(
    git_repo: str, tmp_path, project_id, monkeypatch
):
    """A read-only repo is read where it is; a reference folder through its
    copy (never the folder itself), without the repos inside it or its data."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo, label="web-ui"))
    contracts = _add_reference_repo(sm, project_id, tmp_path, "contracts", "api.yaml", "openapi: 3.0\n")
    sm.set_project_repo_read_only(sm.list_project_repos(project_id)[-1]["id"], True)
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "CLAUDE.md").write_text("contracts is off limits\n")
    (workspace / "docs" / "kb.md").write_text("knowledge\n")
    (workspace / "dump.sql.gz").write_bytes(b"\x1f\x8b data")
    (workspace / "web-ui-checkout" / ".git").mkdir(parents=True)
    ref_id = sm.add_project_repo(project_id, str(workspace), label="workspace", kind="reference")

    _default, roots, read_only, reference = daemon._workspace_roots(sm, sm.get_epic("ENG-1"), git_repo)

    assert read_only == ["contracts", "workspace"] and reference == ["workspace"]
    assert roots["contracts"] == contracts
    copy = roots["workspace"]
    assert copy == os.path.realpath(str(tmp_path / "worktrees" / "_reference" / str(ref_id)))
    assert sorted(
        os.path.relpath(os.path.join(d, f), copy) for d, _, fs in os.walk(copy) for f in fs if not f.startswith(".")
    ) == ["CLAUDE.md", "docs/kb.md"]


async def test_story_brief_maps_every_repo_with_its_prefix(
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

    brief = daemon._build_story_brief(
        sm, sm.get_story("ENG-2"), git_repo, {"web-ui": git_repo, "api": api}, "web-ui"
    )

    assert "### Repo @api" in brief
    assert "Address these files as @api/<path>" in brief
    assert "files_to_create/files_to_modify" in brief  # the plan may name it
    assert "models/: cart.py [Cart]" in brief


async def test_dispatch_hands_every_repo_to_the_planner(
    git_repo: str, tmp_path, project_id, monkeypatch
):
    """End-to-end plumbing: daemon assembles the roots, the graph state
    carries them, and the role receives them. The story's own repo is read
    at its worktree; a reference folder the epic branch cannot be made in
    (this one is not even a git repo) is read where it is."""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2"]])
    repo_id = sm.add_project_repo(project_id, git_repo, label="web-ui")
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Show the cart total")
    api = _add_reference_repo(sm, project_id, tmp_path, "api", "models/cart.py", "class Cart:\n    pass\n")

    import elstin.orchestration.graph as graph_module
    from tests.conftest import fake_run_planner

    seen: dict = {}

    async def capturing_planner(**kwargs):
        seen.update(kwargs)
        return await fake_run_planner(**kwargs)

    monkeypatch.setattr(graph_module, "run_planner", capturing_planner)

    await poll_once(sm, wait=True)

    worktree = sm.get_story("ENG-2")["worktree_path"]
    assert seen["repo_roots"] == {"web-ui": worktree, "api": api}
    assert seen["project_path"] == worktree
    assert seen["default_label"] == "web-ui"
