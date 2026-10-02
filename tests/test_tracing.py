"""LangSmith tracing (forge/services/tracing.py).

Nothing here reaches LangSmith: the SDK client is replaced by one that records
the runs it is handed. What is under test is what a story's run puts into
those runs — the graph's nodes, every model call nested under the node that
made it, every tool call — and that with tracing off nothing is created and
nothing about the run changes.
"""
from __future__ import annotations

import logging

import httpx
import pytest
from langsmith import Client
from langsmith.utils import tracing_is_enabled

import forge.orchestration.graph as graph_module
import forge.services.tracing as tracing
from forge.agents import loop as agent_loop
from forge.config.store import CONFIG_FIELDS, PROJECT_OVERRIDABLE_KEYS, set_config, set_project_config
from forge.daemon.main import _invoke_graph
from forge.llm import openai_compat
from forge.llm.errors import LlmAuthError
from forge.llm.openai_compat import OpenAiCompatProvider
from forge.llm.types import LlmRequest, text_message
from forge.orchestration.graph import checkpointer_from_url, compile_graph
from forge.orchestration.state import initial_state
from forge.services.redaction import redact_value
from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db")

COMPLETION = {
    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "openai/gpt-5.6-terra",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}],
    "usage": {
        "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cost": 0.0021,
        "prompt_tokens_details": {"cached_tokens": 100}, "completion_tokens_details": {"reasoning_tokens": 12},
    },
}


class Recorder(Client):
    """Stands in for the SDK client: keeps the runs it is asked to create and
    update instead of sending them."""

    def __init__(self) -> None:
        super().__init__(api_key="test", api_url="http://langsmith.invalid", auto_batch_tracing=False)
        self.created: dict[str, dict] = {}
        self.updated: dict[str, dict] = {}

    def create_run(self, name, inputs, run_type, **kwargs):
        self.created[str(kwargs.get("id"))] = {
            "name": name, "type": run_type, "inputs": inputs, "parent": str(kwargs.get("parent_run_id")),
            "project": kwargs.get("session_name"), "tags": kwargs.get("tags") or [],
            "metadata": (kwargs.get("extra") or {}).get("metadata") or {},
        }

    def update_run(self, run_id, **kwargs):
        self.updated[str(run_id)] = kwargs

    # --- reading what was recorded ---
    def named(self, name: str) -> list[dict]:
        return [run for run in self.created.values() if run["name"] == name]

    def output_of(self, run: dict) -> dict:
        run_id = next(rid for rid, candidate in self.created.items() if candidate is run)
        return self.updated.get(run_id, {})

    def parent_name(self, run: dict) -> str | None:
        parent = self.created.get(run["parent"])
        return parent["name"] if parent else None


@pytest.fixture(autouse=True)
def _fresh_warnings():
    tracing._warned.clear()


@pytest.fixture()
def langsmith(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Tracing switched on, delivered to a recorder."""
    set_config("langsmith_tracing_enabled", "true")
    set_config("langsmith_api_key", "ls-test-key")
    recorder = Recorder()
    monkeypatch.setattr(tracing, "_client", lambda settings: recorder)
    return recorder


@pytest.fixture()
def recorder_while_off(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Tracing off, but a recorder in place to prove it is never used."""
    recorder = Recorder()
    monkeypatch.setattr(tracing, "_client", lambda settings: recorder)
    return recorder


# --- settings -----------------------------------------------------------------


def test_tracing_is_off_unless_turned_on():
    set_config("langsmith_api_key", "ls-test-key")

    assert tracing.tracing_settings(None) is None


def test_tracing_needs_an_api_key_and_says_so_once(caplog):
    set_config("langsmith_tracing_enabled", "true")

    with caplog.at_level(logging.WARNING, logger="forge.services.tracing"):
        assert tracing.tracing_settings(None) is None
        assert tracing.tracing_settings(None) is None

    assert caplog.text.count("no LangSmith API key") == 1


def test_default_settings():
    set_config("langsmith_tracing_enabled", "true")
    set_config("langsmith_api_key", "  ls-test-key  ")

    settings = tracing.tracing_settings(None)

    assert settings == tracing.TracingSettings(
        api_key="ls-test-key", project="forge", endpoint="https://api.smith.langchain.com", workspace_id=None,
    )


def test_configured_project_endpoint_and_workspace():
    set_config("langsmith_tracing_enabled", "true")
    set_config("langsmith_api_key", "ls-test-key")
    set_config("langsmith_project", "fitpal")
    set_config("langsmith_endpoint", "https://eu.api.smith.langchain.com/")
    set_config("langsmith_workspace_id", "ws-1")

    settings = tracing.tracing_settings(None)

    assert (settings.project, settings.endpoint, settings.workspace_id) == (
        "fitpal", "https://eu.api.smith.langchain.com", "ws-1",
    )


def test_a_project_can_turn_tracing_on_for_itself_and_name_its_own_langsmith_project(project_id):
    other = StateManager().create_project("Other")
    set_config("langsmith_api_key", "ls-test-key")
    set_project_config(project_id, "langsmith_tracing_enabled", "true")
    set_project_config(project_id, "langsmith_project", "just-this-one")

    assert tracing.tracing_settings(project_id).project == "just-this-one"
    assert tracing.tracing_settings(other) is None
    assert tracing.tracing_settings(None) is None


def test_a_project_can_keep_its_code_out_of_langsmith_when_tracing_is_on_globally(project_id):
    set_config("langsmith_tracing_enabled", "true")
    set_config("langsmith_api_key", "ls-test-key")
    set_project_config(project_id, "langsmith_tracing_enabled", "false")

    assert tracing.tracing_settings(project_id) is None
    assert tracing.tracing_settings(None) is not None


def test_the_key_is_a_secret_and_stays_global():
    fields = {field.key: field for field in CONFIG_FIELDS}

    assert fields["langsmith_api_key"].secret is True
    assert fields["langsmith_tracing_enabled"].default == "false"
    assert {"langsmith_tracing_enabled", "langsmith_project"} <= PROJECT_OVERRIDABLE_KEYS
    assert "langsmith_api_key" not in PROJECT_OVERRIDABLE_KEYS


def test_unreadable_settings_mean_no_tracing_not_a_failed_run(monkeypatch, caplog):
    def broken(key, project_id):
        raise RuntimeError("database gone")

    monkeypatch.setattr(tracing, "resolve_bool_config", broken)

    with caplog.at_level(logging.ERROR, logger="forge.services.tracing"):
        assert tracing.tracing_settings(None) is None

    assert "could not read the LangSmith settings" in caplog.text


def test_the_client_is_built_from_the_settings_and_reused(monkeypatch):
    built: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(tracing, "Client", FakeClient)
    monkeypatch.setattr(tracing, "_clients", {})
    settings = tracing.TracingSettings("ls-key", "forge", "https://eu.api.smith.langchain.com", "ws-1")

    first, again = tracing._client(settings), tracing._client(settings)
    other = tracing._client(tracing.TracingSettings("another-key", "forge", "https://eu.api.smith.langchain.com", None))

    assert first is again and first is not other
    # Every client scrubs credentials out of what it sends (forge/services/redaction.py).
    scrub = redact_value
    assert built == [
        {"api_url": "https://eu.api.smith.langchain.com", "api_key": "ls-key", "anonymizer": scrub, "workspace_id": "ws-1"},
        {"api_url": "https://eu.api.smith.langchain.com", "api_key": "another-key", "anonymizer": scrub},
    ]


def test_a_client_that_cannot_be_built_means_no_tracing_not_a_failed_run(monkeypatch, caplog):
    set_config("langsmith_tracing_enabled", "true")
    set_config("langsmith_api_key", "ls-test-key")

    def cannot_build(settings):
        raise ValueError("bad endpoint")

    monkeypatch.setattr(tracing, "_client", cannot_build)

    with caplog.at_level(logging.ERROR, logger="forge.services.tracing"):
        with tracing.traced_run(None, name="forge:ENG-2") as extra:
            assert extra == {}

    assert "could not set up the LangSmith client" in caplog.text


# --- the switch ---------------------------------------------------------------


def test_off_means_off_even_when_the_environment_says_otherwise(monkeypatch):
    """forge's settings are the only switch: an ambient LANGSMITH_TRACING does
    not turn on tracing of a run."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    with tracing.traced_run(None, name="forge:ENG-2") as extra:
        assert extra == {}
        assert tracing_is_enabled() is False
    with tracing.traced_operation(None, name="forge:lessons:ENG-2"):
        assert tracing_is_enabled() is False


def test_on_hands_the_run_a_tracer_and_its_labels(langsmith):
    with tracing.traced_run(None, name="forge:ENG-2", metadata={"story_key": "ENG-2"}, tags=["forge", "dev"]) as extra:
        assert tracing_is_enabled() is True

    assert extra["run_name"] == "forge:ENG-2"
    assert extra["metadata"] == {"story_key": "ENG-2"}
    assert set(extra["tags"]) == {"forge", "dev"}
    assert len(extra["callbacks"]) == 1
    assert extra["callbacks"][0].project_name == "forge"
    assert extra["callbacks"][0].client is langsmith


# --- one story run ------------------------------------------------------------


EPIC = {"jira_key": "ENG-1"}
STORY = {"jira_key": "ENG-2", "kind": "dev", "summary": "Add a widget", "retry_count": 1}


async def _run_graph(postgres_url: str, project_id: int, *, resume: bool = False) -> dict:
    state = initial_state("ENG-2", "Add a widget", "/repo")
    state["worktree_path"] = "/wt"
    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        return await _invoke_graph(
            graph, state, {"configurable": {"thread_id": "ENG-2"}},
            epic={**EPIC, "project_id": project_id}, story=STORY, resume=resume,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_agents")
async def test_a_story_run_is_one_trace_with_a_child_per_node(langsmith, postgres_url, project_id):
    result = await _run_graph(postgres_url, project_id)

    assert result["status"] == "completed"  # tracing does not change what the run does
    [root] = langsmith.named("forge:ENG-2")
    assert root["parent"] == "None"
    assert root["metadata"] | {"thread_id": "ENG-2"} == root["metadata"]
    assert root["metadata"]["story_key"] == "ENG-2"
    assert root["metadata"]["epic_key"] == "ENG-1"
    assert root["metadata"]["kind"] == "dev"
    assert root["metadata"]["resume"] is False
    assert root["metadata"]["retry_count"] == 1
    assert root["metadata"]["ticket"] == "Add a widget"
    assert set(root["tags"]) == {"forge", "dev", "fresh"}
    for node in ("planner", "dev", "gate", "verifier"):
        [run] = langsmith.named(node)
        assert langsmith.parent_name(run) == "forge:ENG-2", node
    assert {run["project"] for run in langsmith.created.values()} == {"forge"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_agents")
async def test_a_resumed_run_is_labelled_as_one(langsmith, postgres_url, project_id):
    await _run_graph(postgres_url, project_id, resume=True)

    [root] = langsmith.named("forge:ENG-2")
    assert root["metadata"]["resume"] is True
    assert set(root["tags"]) == {"forge", "dev", "resume"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_agents")
async def test_a_project_named_in_settings_receives_the_trace(langsmith, postgres_url, project_id):
    set_project_config(project_id, "langsmith_project", "fitpal")

    await _run_graph(postgres_url, project_id)

    assert {run["project"] for run in langsmith.created.values()} == {"fitpal"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_agents")
async def test_with_tracing_off_nothing_is_created_and_the_run_is_the_same(recorder_while_off, postgres_url, project_id):
    result = await _run_graph(postgres_url, project_id)

    assert result["status"] == "completed"
    assert recorder_while_off.created == {}


# --- model calls --------------------------------------------------------------


def _provider(monkeypatch: pytest.MonkeyPatch, handler) -> OpenAiCompatProvider:
    """The real adapter, its HTTP traffic answered by `handler`."""
    real_client = openai_compat.AsyncOpenAI
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        openai_compat, "AsyncOpenAI", lambda **kwargs: real_client(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    return OpenAiCompatProvider(api_key="k", base_url="http://llm.invalid/v1")


def _request() -> LlmRequest:
    return LlmRequest(model="openai/gpt-5.6-terra", messages=[text_message("user", "Plan it.")], effort="high")


@pytest.mark.asyncio
async def test_a_model_call_is_recorded_under_the_node_that_made_it(langsmith, postgres_url, project_id, monkeypatch):
    provider = _provider(monkeypatch, lambda request: httpx.Response(200, json=COMPLETION))
    replies = []

    async def planner(**kwargs):
        replies.append(await provider.complete(_request()))
        return {"questions": [], "context_bundle": {"k": 1}, "plan": {"summary": "s"}, "acceptance_criteria": [], "messages": []}

    async def dev(**kwargs):
        return {"file_changes": [], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {}}

    async def gate(**kwargs):
        return {"passed": True, "issues": [], "required_fixes": []}

    async def verifier(**kwargs):
        return {"verifier_report": {"passed": True}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_planner", planner)
    monkeypatch.setattr(graph_module, "run_dev", dev)
    monkeypatch.setattr(graph_module, "run_deterministic_gate", gate)
    monkeypatch.setattr(graph_module, "run_verifier", verifier)

    await _run_graph(postgres_url, project_id)

    # what the caller gets is what it always got
    assert replies[0].text() == "hello"
    assert replies[0].usage.cost_usd == pytest.approx(0.0021)
    [call] = [run for run in langsmith.created.values() if run["type"] == "llm"]
    assert langsmith.parent_name(call) == "planner"
    assert call["project"] == "forge"
    assert call["metadata"]["ls_model_name"] == "openai/gpt-5.6-terra"
    assert call["metadata"]["ls_invocation_params"] == {"reasoning_effort": "high"}
    assert call["inputs"]["messages"][0]["content"] == "Plan it."
    usage = langsmith.output_of(call)["outputs"]["usage_metadata"]
    assert (usage["input_tokens"], usage["output_tokens"]) == (120, 30)
    assert usage["input_token_details"] == {"cache_read": 100}
    assert usage["output_token_details"] == {"reasoning": 12}


@pytest.mark.asyncio
async def test_a_provider_error_is_still_the_same_typed_error_and_the_span_records_it(langsmith, monkeypatch):
    provider = _provider(
        monkeypatch, lambda request: httpx.Response(401, json={"error": {"message": "bad key", "code": "invalid_api_key"}}),
    )

    with tracing.traced_operation(None, name="forge:probe"):
        with pytest.raises(LlmAuthError):
            await provider.complete(_request())

    [call] = [run for run in langsmith.created.values() if run["type"] == "llm"]
    assert "bad key" in (langsmith.output_of(call).get("error") or "")


@pytest.mark.asyncio
async def test_a_model_call_outside_a_traced_run_records_nothing(recorder_while_off, monkeypatch):
    provider = _provider(monkeypatch, lambda request: httpx.Response(200, json=COMPLETION))

    reply = await provider.complete(_request())

    assert reply.text() == "hello"
    assert recorder_while_off.created == {}


# --- tool calls ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_call_is_a_span_and_the_result_is_unchanged(langsmith):
    def read_file(path: str) -> str:
        return f"contents of {path}"

    def explode(**kwargs) -> str:
        raise RuntimeError("disk on fire")

    tools = {"read_file": read_file, "explode": explode}

    with tracing.traced_operation(None, name="forge:probe"):
        assert await agent_loop._call_tool(tools, "read_file", {"path": "a.py"}) == ("contents of a.py", False)
        text, is_error = await agent_loop._call_tool(tools, "explode", {})
        assert (is_error, "disk on fire" in text) == (True, True)
        assert (await agent_loop._call_tool(tools, "nope", {}))[1] is True

    [ok] = langsmith.named("read_file")
    assert ok["type"] == "tool"
    assert langsmith.parent_name(ok) == "forge:probe"
    assert ok["inputs"] == {"path": "a.py"}
    assert langsmith.output_of(ok)["outputs"] == {"output": "contents of a.py", "is_error": False}
    assert langsmith.output_of(ok).get("error") is None
    [failed] = langsmith.named("explode")
    assert "disk on fire" in langsmith.output_of(failed)["error"]
    assert len(langsmith.named("nope")) == 1


@pytest.mark.asyncio
async def test_a_tool_call_with_tracing_off_records_nothing(recorder_while_off):
    assert await agent_loop._call_tool({"t": lambda: "done"}, "t", {}) == ("done", False)
    assert recorder_while_off.created == {}


def test_a_long_tool_input_or_output_is_cut_and_says_so():
    long = "x" * (tracing.MAX_TRACED_CHARS + 500)

    clipped = tracing._clip({"path": "a.py", "content": long, "nested": [long, 3], "n": 7})

    assert clipped["path"] == "a.py" and clipped["n"] == 7
    assert clipped["content"].startswith("x" * tracing.MAX_TRACED_CHARS)
    assert clipped["content"].endswith("… [500 more characters not traced]")
    assert clipped["nested"][0] == clipped["content"] and clipped["nested"][1] == 3


# --- outside the graph --------------------------------------------------------


def test_an_operation_is_a_trace_of_its_own(langsmith):
    with tracing.traced_operation(None, name="forge:lessons:ENG-2", metadata={"story_key": "ENG-2"}, tags=["forge", "lessons"]):
        pass

    [run] = langsmith.named("forge:lessons:ENG-2")
    assert run["parent"] == "None" and run["project"] == "forge"
    assert run["metadata"]["story_key"] == "ENG-2" and set(run["tags"]) == {"forge", "lessons"}


def test_an_operation_that_raises_still_raises_and_is_recorded_as_failed(langsmith):
    with pytest.raises(ValueError, match="boom"):
        with tracing.traced_operation(None, name="forge:lessons:ENG-2"):
            raise ValueError("boom")

    [run] = langsmith.named("forge:lessons:ENG-2")
    assert "boom" in langsmith.output_of(run)["error"]


# --- the settings screen ------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from forge.app import create_app

    return TestClient(create_app())


def test_the_settings_api_lists_the_tracing_fields_and_masks_the_key():
    with _client() as client:
        listed = {item["key"]: item for item in client.get("/api/config").json()}
        assert {
            "langsmith_tracing_enabled", "langsmith_api_key", "langsmith_project", "langsmith_endpoint",
            "langsmith_workspace_id",
        } <= set(listed)
        assert listed["langsmith_tracing_enabled"]["value"] == "false"
        assert listed["langsmith_project"]["value"] == "forge"

        assert client.put("/api/config/langsmith_api_key", json={"value": "ls-secret-123"}).status_code == 200
        after = {item["key"]: item for item in client.get("/api/config").json()}

    assert after["langsmith_api_key"]["value"] == "••••••••" and after["langsmith_api_key"]["is_set"] is True
    assert "ls-secret-123" not in str(after)


def test_a_project_screen_offers_the_tracing_switch_but_not_the_key(project_id):
    with _client() as client:
        listed = {item["key"] for item in client.get(f"/api/projects/{project_id}/config").json()}
        turned_on = client.put(f"/api/projects/{project_id}/config/langsmith_tracing_enabled", json={"value": "true"})
        refused = client.put(f"/api/projects/{project_id}/config/langsmith_api_key", json={"value": "ls-secret-123"})

    assert {"langsmith_tracing_enabled", "langsmith_project"} <= listed
    assert "langsmith_api_key" not in listed
    assert turned_on.status_code == 200
    assert refused.status_code >= 400
