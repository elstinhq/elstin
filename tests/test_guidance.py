"""A person's message to an agent that is already working.

Cancel used to be the only control on a running story. From the bottom up: the
queue, the loop that reads it at the start of a turn, the roles that wire it in,
the route that fills it, and the daemon that clears what was never read."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

import forge.agents.roles.dev_loop as dev_loop_module
import forge.agents.roles.planner_loop as planner_module
import forge.daemon.main as daemon
import forge.orchestration.graph as graph_module
from forge.agents.loop import LoopConfig, run_agent_loop
from forge.agents.roles.dev_loop import run_dev
from forge.agents.roles.planner_loop import run_planner
from forge.config.store import set_config
from forge.llm.base import Capabilities
from forge.llm.types import LlmRequest, LlmResponse, TextBlock, ToolCallBlock, Usage, text_message
from forge.services.guidance import MAX_GUIDANCE_CHARS, render_guidance, take_guidance
from forge.services.run_activity import list_activity
from forge.state.manager import StateManager
from forge.state.manager_story import MAX_PENDING_GUIDANCE, GuidanceQueueFull

pytestmark = pytest.mark.usefixtures("forge_db")


@pytest.fixture()
def board(project_id) -> StateManager:
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", kind="dev")
    return sm


@dataclass
class FakeProvider:
    script: list[LlmResponse | Exception]
    name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list[LlmRequest] = field(default_factory=list)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _text(text: str) -> LlmResponse:
    return LlmResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5), model="m")


def _tool_call(name: str, arguments: dict) -> LlmResponse:
    return LlmResponse(
        content=[ToolCallBlock(id="c1", name=name, arguments=arguments)], stop_reason="tool_calls",
        usage=Usage(input_tokens=10, output_tokens=5), model="m",
    )


def _user_texts(request: LlmRequest) -> list[str]:
    return [b.text for m in request.messages if m.role == "user" for b in m.content if isinstance(b, TextBlock)]


# --- the queue ------------------------------------------------------------------


def test_messages_come_out_oldest_first_and_only_once(board):
    assert board.add_story_guidance("ENG-2", "first") == 1
    assert board.add_story_guidance("ENG-2", "second") == 2

    assert [m["text"] for m in board.take_story_guidance("ENG-2")] == ["first", "second"]
    assert board.take_story_guidance("ENG-2") == []
    assert board.get_story("ENG-2")["jira_key"] == "ENG-2"


def test_an_unknown_story_has_no_queue(board):
    assert board.add_story_guidance("NOPE-1", "hello") is None
    assert board.take_story_guidance("NOPE-1") == []


def test_a_queue_nobody_is_reading_stops_growing(board):
    for n in range(MAX_PENDING_GUIDANCE):
        board.add_story_guidance("ENG-2", f"message {n}")
    with pytest.raises(GuidanceQueueFull):
        board.add_story_guidance("ENG-2", "one too many")
    assert len(board.take_story_guidance("ENG-2")) == MAX_PENDING_GUIDANCE


def test_each_message_is_stamped(board):
    board.add_story_guidance("ENG-2", "hello")
    [message] = board.take_story_guidance("ENG-2")
    assert message["text"] == "hello" and message["at"]


# --- what the agent is shown -----------------------------------------------------


def test_the_message_is_framed_as_advice_that_does_not_widen_dev_scope():
    shown = render_guidance("  use the existing helper  ", phase="dev")
    assert shown.endswith("use the existing helper")
    assert "only the files in the approved plan" in shown


def test_the_planner_is_told_it_only_reads_and_plans():
    assert "only reading the code and writing the plan" in render_guidance("x", phase="planner")


def test_taking_guidance_removes_it_and_says_so_in_the_activity_feed(board):
    board.add_story_guidance("ENG-2", "one")
    board.add_story_guidance("ENG-2", "two")

    shown = take_guidance("ENG-2", phase="dev")

    assert len(shown) == 2 and shown[0].endswith("one") and shown[1].endswith("two")
    assert take_guidance("ENG-2", phase="dev") == []
    assert "The agent read 2 messages from a person" in [e["title"] for e in list_activity("ENG-2")]


def test_a_queue_that_cannot_be_read_costs_the_run_nothing(monkeypatch):
    def broken(self, key):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(StateManager, "take_story_guidance", broken)
    assert take_guidance("ENG-2", phase="dev") == []


# --- the loop ----------------------------------------------------------------------


async def test_a_message_joins_the_conversation_at_the_start_of_the_next_turn_and_stays():
    polls = {"n": 0}

    def pending() -> list[str]:
        polls["n"] += 1
        return ["use the existing helper"] if polls["n"] == 2 else []  # queued between turn one and turn two

    provider = FakeProvider(script=[_tool_call("noop", {}), _text("done")])
    config = LoopConfig(provider=provider, model="m", system="s", pending_messages=pending, tool_fns={"noop": lambda: "ok"})

    result = await run_agent_loop(config, [text_message("user", "go")])

    assert polls["n"] == 2  # asked once per turn
    assert "use the existing helper" not in _user_texts(provider.calls[0])
    assert _user_texts(provider.calls[1]).count("use the existing helper") == 1
    kept = [b.text for m in result.history if m.role == "user" for b in m.content if isinstance(b, TextBlock)]
    assert "use the existing helper" in kept  # a resumed run still has it


async def test_the_message_comes_before_the_turns_trailing_reminder():
    provider = FakeProvider(script=[_text("done")])
    config = LoopConfig(
        provider=provider, model="m", system="s",
        pending_messages=lambda: ["MESSAGE"], trailing_reminder=lambda: "REMINDER",
    )
    await run_agent_loop(config, [text_message("user", "go")])
    assert _user_texts(provider.calls[0]) == ["go", "MESSAGE", "REMINDER"]


async def test_a_hook_that_raises_never_costs_the_run():
    def broken() -> list[str]:
        raise RuntimeError("boom")

    provider = FakeProvider(script=[_text("done")])
    result = await run_agent_loop(
        LoopConfig(provider=provider, model="m", system="s", pending_messages=broken), [text_message("user", "go")],
    )
    assert result.is_error is False and result.final_text == "done"


# --- the roles -----------------------------------------------------------------------


async def test_dev_reads_a_message_queued_for_its_run(board, git_repo, monkeypatch):
    set_config("openai_api_key", "sk-test")
    board.add_story_guidance("ENG-2", "prefer the existing formatDate helper")
    provider = FakeProvider(script=[_text("nothing to change")])
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *a, **k: provider)

    await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo, run_id="ENG-2")

    assert any("prefer the existing formatDate helper" in t for t in _user_texts(provider.calls[0]))
    assert board.take_story_guidance("ENG-2") == []


async def test_the_planner_reads_one_too(board, git_repo, monkeypatch):
    set_config("openai_api_key", "sk-test")
    set_config("planner_explorers_enabled", "false")
    board.add_story_guidance("ENG-2", "the cart lives in packages/cart")
    provider = FakeProvider(script=[_text("no plan")])
    monkeypatch.setattr(planner_module, "get_provider", lambda *a, **k: provider)

    with pytest.raises(Exception):  # the scripted reply is not a plan; only the first request matters here
        await run_planner(user_request="Add a widget", project_path=git_repo, run_id="ENG-2")

    assert any("the cart lives in packages/cart" in t for t in _user_texts(provider.calls[0]))


async def test_a_run_with_no_id_has_no_queue_to_read(git_repo, monkeypatch):
    set_config("openai_api_key", "sk-test")
    provider = FakeProvider(script=[_text("nothing to change")])
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *a, **k: provider)
    await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)
    assert len(provider.calls) == 1


# --- the route -----------------------------------------------------------------------


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


@pytest.fixture()
def running(board):
    """A story whose agent run is in flight in this process."""
    board.set_story_status("ENG-2", "dispatched")
    assert daemon.claim_run("ENG-2")
    yield board
    daemon.release_run("ENG-2")


@pytest.mark.usefixtures("fake_agents")
def test_a_message_to_a_running_agent_is_queued_and_shown_in_the_feed(running):
    with _client() as client:
        response = client.post("/api/stories/ENG-2/guidance", json={"message": "  use the helper  "})

    assert response.status_code == 200
    assert response.json() == {"story_key": "ENG-2", "pending": 1}
    assert [m["text"] for m in running.take_story_guidance("ENG-2")] == ["use the helper"]
    sent = [e for e in list_activity("ENG-2") if e["title"] == "Message sent to the agent"]
    assert sent and sent[0]["detail"] == "use the helper"


@pytest.mark.usefixtures("fake_agents")
def test_a_credential_pasted_into_a_message_is_not_kept_in_the_feed(running):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
    with _client() as client:
        client.post("/api/stories/ENG-2/guidance", json={"message": f"token is {token}"})
    assert token not in repr(list_activity("ENG-2"))


@pytest.mark.usefixtures("fake_agents")
def test_an_unknown_story_is_a_404(board):
    with _client() as client:
        assert client.post("/api/stories/NOPE-1/guidance", json={"message": "hi"}).status_code == 404


@pytest.mark.usefixtures("fake_agents")
@pytest.mark.parametrize("status", ["pending", "awaiting_plan_approval", "ready_to_merge", "merging", "merged", "failed"])
def test_a_story_with_no_agent_running_has_nobody_to_message(board, status):
    board.set_story_status("ENG-2", status)
    with _client() as client:
        response = client.post("/api/stories/ENG-2/guidance", json={"message": "hi"})
    assert response.status_code == 409 and "no agent run in progress" in response.json()["detail"]
    assert board.take_story_guidance("ENG-2") == []


@pytest.mark.usefixtures("fake_agents")
def test_a_dispatched_story_whose_run_is_not_in_this_process_is_refused(board):
    """Status alone is not enough: a crashed daemon leaves a story "dispatched"
    with nothing reading its queue."""
    board.set_story_status("ENG-2", "dispatched")
    with _client() as client:
        assert client.post("/api/stories/ENG-2/guidance", json={"message": "hi"}).status_code == 409


@pytest.mark.usefixtures("fake_agents")
def test_a_qa_story_has_no_agent_to_message(running):
    running.set_story_kind("ENG-2", "qa")
    with _client() as client:
        response = client.post("/api/stories/ENG-2/guidance", json={"message": "hi"})
    assert response.status_code == 409 and "QA story" in response.json()["detail"]


@pytest.mark.usefixtures("fake_agents")
@pytest.mark.parametrize("message", ["", "   ", "x" * (MAX_GUIDANCE_CHARS + 1)])
def test_an_empty_or_oversized_message_is_refused(running, message):
    with _client() as client:
        assert client.post("/api/stories/ENG-2/guidance", json={"message": message}).status_code == 422


@pytest.mark.usefixtures("fake_agents")
def test_a_queue_nobody_is_reading_is_a_409_not_unbounded_growth(running):
    with _client() as client:
        for n in range(MAX_PENDING_GUIDANCE):
            assert client.post("/api/stories/ENG-2/guidance", json={"message": f"m{n}"}).status_code == 200
        response = client.post("/api/stories/ENG-2/guidance", json={"message": "one more"})
    assert response.status_code == 409 and "has not read" in response.json()["detail"]


# --- the daemon ------------------------------------------------------------------------


def test_what_the_agent_never_read_is_cleared_and_reported_with_its_text(board):
    board.add_story_guidance("ENG-2", "too late for this run")

    daemon._discard_unread_guidance(board, "ENG-2")

    assert board.take_story_guidance("ENG-2") == []
    [event] = [e for e in list_activity("ENG-2") if "not read" in e["title"]]
    assert event["title"] == "1 message to the agent arrived after its last turn and was not read"
    assert event["detail"] == "too late for this run"


def test_nothing_unread_means_nothing_reported(board):
    daemon._discard_unread_guidance(board, "ENG-2")
    assert not [e for e in list_activity("ENG-2") if "not read" in e["title"]]


@pytest.mark.usefixtures("fake_agents")
async def test_a_run_that_ends_with_a_message_unread_clears_it_so_a_later_pass_does_not_find_it_stale(
    git_repo, tmp_path, project_id,
):
    set_config("openai_api_key", "sk-test")
    set_config("worktrees_base_dir", str(tmp_path / "wt"))
    set_config("require_plan_approval", "false")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", depends_on=[])
    sm.add_story_guidance("ENG-2", "queued before the run; the fake agents never read it")

    await daemon.poll_once(sm, wait=True)

    assert sm.get_story("ENG-2")["status"] == "ready_to_merge"
    assert sm.take_story_guidance("ENG-2") == []
    assert any("not read" in e["title"] for e in list_activity("ENG-2"))
