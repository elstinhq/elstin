"""The model side of a project: which provider, endpoint and key its calls use,
and which model each role runs on.

A project may choose any of these; whatever it does not choose is the global
setting, so it can move one role, or just the key, without pinning the rest.
The graph carries only the project's id (never a key — graph state is
checkpointed and traced), and each role resolves from it where it is used. These
tests follow a choice from the settings API to the call it changes. The last part
covers the picker's suggestions: the provider's own model listing, and what is
offered without it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import elstin.orchestration.graph as graph_module
import elstin.services.generation as generation
import elstin.services.model_options as model_options
from elstin.config.store import (
    CONFIG_FIELDS,
    PROJECT_OVERRIDABLE_KEYS,
    project_override,
    resolve_text,
    set_config,
    set_project_config,
)
from elstin.daemon import main as daemon
from elstin.llm import openai_compat
from elstin.llm.base import Capabilities
from elstin.llm.errors import LlmAuthError, LlmConnectionError
from elstin.llm.openai_compat import OpenAiCompatProvider
from elstin.llm.pricing import known_models
from elstin.llm.registry import LlmNotConfiguredError, get_provider
from elstin.llm.types import LlmRequest, LlmResponse, TextBlock, Usage
from elstin.orchestration.graph import checkpointer_from_url, compile_graph
from elstin.orchestration.reconciler import DispatchStory, ResumeStory
from elstin.orchestration.state import initial_state
from elstin.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("elstin_db")

MODEL_KEYS = ("planner_sdk_model", "planner_explorer_model", "po_model", "dev_sdk_model", "verifier_qa_model")
CONNECTION_KEYS = ("llm_provider", "llm_base_url", "openai_api_key")


def _client() -> TestClient:
    from elstin.app import create_app

    return TestClient(create_app())


# --- resolution ---------------------------------------------------------------


def test_the_model_and_connection_settings_are_overridable_and_the_models_are_typed_as_models():
    assert {field.key for field in CONFIG_FIELDS if field.type == "model"} == set(MODEL_KEYS)
    assert set(MODEL_KEYS) | set(CONNECTION_KEYS) <= PROJECT_OVERRIDABLE_KEYS


def test_a_project_with_no_choice_uses_the_global_setting(project_id):
    set_config("dev_sdk_model", "global-dev")

    assert resolve_text("dev_sdk_model", project_id) == "global-dev"
    assert resolve_text("dev_sdk_model", None) == "global-dev"


def test_a_projects_choice_applies_to_that_project_only(project_id):
    other = StateManager().create_project("Other")
    set_config("dev_sdk_model", "global-dev")
    set_project_config(project_id, "dev_sdk_model", "project-dev")

    assert resolve_text("dev_sdk_model", project_id) == "project-dev"
    assert resolve_text("dev_sdk_model", other) == "global-dev"
    assert resolve_text("dev_sdk_model", None) == "global-dev"


def test_a_project_can_move_one_role_and_leave_the_others_on_the_global_setting(project_id):
    set_config("planner_sdk_model", "global-planner")
    set_config("dev_sdk_model", "global-dev")
    set_project_config(project_id, "planner_sdk_model", "project-planner")

    set_config("dev_sdk_model", "global-dev-changed-later")

    assert resolve_text("planner_sdk_model", project_id) == "project-planner"
    assert resolve_text("dev_sdk_model", project_id) == "global-dev-changed-later"  # follows the global


def test_a_blank_choice_is_no_choice_never_an_empty_value(project_id):
    set_config("dev_sdk_model", "global-dev")
    set_project_config(project_id, "dev_sdk_model", "   ")

    assert resolve_text("dev_sdk_model", project_id) == "global-dev"
    assert project_override("dev_sdk_model", project_id) is None


def test_a_setting_nobody_set_resolves_to_none_for_the_caller_to_default(project_id):
    assert resolve_text("planner_explorer_model", project_id) is None  # no global default either


# --- the provider a project's calls go to ---------------------------------------


def _endpoint_of(provider) -> tuple[str, str]:
    client = provider._client
    return client.api_key, str(client.base_url)


def test_a_project_with_no_connection_choice_uses_the_global_provider(project_id):
    set_config("openai_api_key", "global-key")
    set_config("llm_base_url", "https://global.example/v1")

    assert _endpoint_of(get_provider(project_id)) == ("global-key", "https://global.example/v1/")
    assert _endpoint_of(get_provider()) == ("global-key", "https://global.example/v1/")


def test_a_project_with_only_its_own_key_uses_it_on_the_global_endpoint(project_id):
    """Another account on the same endpoint — separate billing per project."""
    set_config("openai_api_key", "global-key")
    set_config("llm_base_url", "https://global.example/v1")
    set_project_config(project_id, "openai_api_key", "project-key")

    assert _endpoint_of(get_provider(project_id)) == ("project-key", "https://global.example/v1/")
    assert _endpoint_of(get_provider(None))[0] == "global-key"


def test_a_project_with_its_own_endpoint_and_key_uses_both(project_id):
    set_config("openai_api_key", "global-key")
    set_config("llm_base_url", "https://global.example/v1")
    set_project_config(project_id, "llm_base_url", "https://project.example/v1")
    set_project_config(project_id, "openai_api_key", "project-key")

    assert _endpoint_of(get_provider(project_id)) == ("project-key", "https://project.example/v1/")


def test_the_global_key_is_never_sent_to_a_projects_own_endpoint(project_id):
    """The global key was issued for the global endpoint. A project that points
    somewhere else must bring its own key, not borrow this one."""
    set_config("openai_api_key", "global-key")
    set_project_config(project_id, "llm_base_url", "https://project.example/v1")

    with pytest.raises(LlmNotConfiguredError, match="no API key of its own"):
        get_provider(project_id)


def test_a_blank_endpoint_choice_is_not_an_endpoint_choice(project_id):
    set_config("openai_api_key", "global-key")
    set_project_config(project_id, "llm_base_url", "  ")

    assert _endpoint_of(get_provider(project_id))[0] == "global-key"  # still the global one, so its key is fine


def test_a_project_naming_another_provider_needs_its_own_key_and_the_provider_must_exist(project_id):
    set_config("openai_api_key", "global-key")
    set_project_config(project_id, "llm_provider", "somewhere-else")
    with pytest.raises(LlmNotConfiguredError, match="no API key of its own"):
        get_provider(project_id)

    set_project_config(project_id, "openai_api_key", "project-key")
    with pytest.raises(LlmNotConfiguredError, match="Unknown llm_provider"):
        get_provider(project_id)


def test_naming_the_same_provider_as_the_global_one_is_not_a_move(project_id):
    set_config("openai_api_key", "global-key")
    set_project_config(project_id, "llm_provider", "openai")

    assert _endpoint_of(get_provider(project_id))[0] == "global-key"


def test_with_no_key_anywhere_the_error_says_so(project_id):
    with pytest.raises(LlmNotConfiguredError, match="No OpenAI API key configured"):
        get_provider(project_id)


def test_one_projects_connection_does_not_leak_into_another(project_id):
    other = StateManager().create_project("Other")
    set_config("openai_api_key", "global-key")
    set_project_config(project_id, "llm_base_url", "https://project.example/v1")
    set_project_config(project_id, "openai_api_key", "project-key")

    assert _endpoint_of(get_provider(other)) == ("global-key", "https://api.openai.com/v1/")


# --- the project's settings API -----------------------------------------------


def _listed(client, project_id) -> dict[str, dict]:
    return {item["key"]: item for item in client.get(f"/api/projects/{project_id}/config").json()}


def test_a_project_picks_a_model_and_can_go_back_to_the_global_one(project_id):
    set_config("dev_sdk_model", "global-dev")

    with _client() as client:
        listed = _listed(client, project_id)
        assert set(MODEL_KEYS) <= set(listed)
        assert (listed["dev_sdk_model"]["type"], listed["dev_sdk_model"]["overridden"]) == ("model", False)
        assert listed["dev_sdk_model"]["value"] == "global-dev"  # what it inherits

        put = client.put(f"/api/projects/{project_id}/config/dev_sdk_model", json={"value": "project-dev"})
        picked = _listed(client, project_id)["dev_sdk_model"]

        client.delete(f"/api/projects/{project_id}/config/dev_sdk_model")
        reset = _listed(client, project_id)["dev_sdk_model"]

    assert put.status_code == 200
    assert (picked["value"], picked["overridden"]) == ("project-dev", True)
    assert (reset["value"], reset["overridden"]) == ("global-dev", False)


def test_a_project_sets_its_provider_endpoint_and_key_and_the_key_is_never_shown(project_id):
    with _client() as client:
        for key, value in (
            ("llm_base_url", "https://project.example/v1"), ("openai_api_key", "sk-project-secret-123"),
        ):
            assert client.put(f"/api/projects/{project_id}/config/{key}", json={"value": value}).status_code == 200
        listed = _listed(client, project_id)

    assert set(CONNECTION_KEYS) <= set(listed)
    assert listed["llm_base_url"]["value"] == "https://project.example/v1"
    assert (listed["openai_api_key"]["value"], listed["openai_api_key"]["overridden"]) == ("••••••••", True)
    assert "sk-project-secret-123" not in str(listed)
    assert get_provider(project_id)._client.api_key == "sk-project-secret-123"  # stored, and used


# --- from the daemon into the graph -------------------------------------------


class _RecordingGraph:
    """Stands in for the compiled graph: keeps what the daemon hands it."""

    def __init__(self) -> None:
        self.state: dict | None = None
        self.patch: dict | None = None

    async def aget_state(self, config):
        return SimpleNamespace(next=(), values={})

    async def aupdate_state(self, config, patch, as_node=None):
        self.patch = patch

    async def ainvoke(self, state, config):
        self.state = state
        return {"status": "awaiting_approval"}


def _story_in_project(project_id, git_repo, tmp_path, *, status: str) -> StateManager:
    set_config("worktrees_base_dir", str(tmp_path / "wt"))
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")
    sm.set_story_status("ENG-2", status)
    return sm


@pytest.mark.asyncio
async def test_a_fresh_run_carries_its_project_and_never_a_key(project_id, git_repo, tmp_path, monkeypatch):
    sm = _story_in_project(project_id, git_repo, tmp_path, status="pending")
    set_project_config(project_id, "openai_api_key", "sk-project-secret-123")
    graph = _RecordingGraph()
    monkeypatch.setattr(daemon, "compile_graph", lambda *args, **kwargs: graph)

    await daemon.handle_intent(sm, sm.get_epic("ENG-1"), DispatchStory("ENG-2"))

    assert graph.state["project_id"] == project_id
    assert "sk-project-secret-123" not in str(graph.state)  # the state is checkpointed and traced


@pytest.mark.asyncio
async def test_a_resumed_run_carries_its_project_too(project_id, git_repo, tmp_path, monkeypatch):
    sm = _story_in_project(project_id, git_repo, tmp_path, status="awaiting_plan_approval")
    sm.set_story_gate_approved("ENG-2")
    graph = _RecordingGraph()
    monkeypatch.setattr(daemon, "compile_graph", lambda *args, **kwargs: graph)

    await daemon.handle_intent(sm, sm.get_epic("ENG-1"), ResumeStory("ENG-2"))

    assert graph.patch["project_id"] == project_id  # also for a run checkpointed before it was carried


async def _run_graph_capturing_roles(database_url: str, monkeypatch, **state_fields) -> dict[str, dict]:
    """Runs the real graph with the roles replaced by recorders; returns the
    keyword arguments each one was called with."""
    captured: dict[str, dict] = {}
    fakes = {
        "run_planner": (
            "planner",
            {"questions": [], "context_bundle": {"k": 1}, "plan": {"summary": "s"}, "acceptance_criteria": [], "messages": []},
        ),
        "run_dev": ("dev", {"file_changes": [], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {}}),
        "run_deterministic_gate": ("gate", {"passed": True, "issues": [], "required_fixes": []}),
        "run_verifier": ("verifier", {"verifier_report": {"passed": True}, "diffs": [], "messages": []}),
    }
    for attribute, (name, result) in fakes.items():
        def make(name=name, result=result):
            async def role(**kwargs):
                captured[name] = kwargs
                return result
            return role
        monkeypatch.setattr(graph_module, attribute, make())

    state = initial_state("ENG-2", "Add a widget", "/repo")
    state["worktree_path"] = "/wt"
    state.update(state_fields)
    async with checkpointer_from_url(database_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await graph.ainvoke(state, {"configurable": {"thread_id": "ENG-2"}})
    return captured


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_agents")
async def test_every_role_is_told_which_project_it_works_for(database_url, monkeypatch):
    roles = await _run_graph_capturing_roles(database_url, monkeypatch, project_id=7)

    assert {name: roles[name]["project_id"] for name in ("planner", "dev", "verifier")} == {
        "planner": 7, "dev": 7, "verifier": 7,
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_agents")
async def test_a_run_checkpointed_before_projects_could_choose_uses_the_global_settings(database_url, monkeypatch):
    roles = await _run_graph_capturing_roles(database_url, monkeypatch)

    assert all(roles[name]["project_id"] is None for name in ("planner", "dev", "verifier"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_agents")
async def test_the_older_shared_model_override_still_reaches_planner_and_dev(database_url, monkeypatch):
    roles = await _run_graph_capturing_roles(database_url, monkeypatch, sdk_model="shared")

    assert roles["planner"]["sdk_model"] == roles["dev"]["sdk_model"] == "shared"


# --- the single-shot calls: review, lessons, guides -----------------------------


@dataclass
class _Recorder:
    name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)
    model_seen: str | None = None

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.model_seen = request.model
        return LlmResponse(
            content=[TextBlock(text='{"passed": true}')], stop_reason="end_turn",
            usage=Usage(input_tokens=5, output_tokens=2), model=request.model,
        )


@pytest.mark.asyncio
async def test_the_review_call_uses_the_projects_provider_and_review_model(project_id, monkeypatch):
    set_config("verifier_qa_model", "global-review")
    set_project_config(project_id, "verifier_qa_model", "project-review")
    asked, recorder = [], _Recorder()
    monkeypatch.setattr(generation, "get_provider", lambda project=None: asked.append(project) or recorder)
    messages = [{"role": "user", "content": "review"}]

    await generation.chat_completion_json(messages=messages, project_id=project_id)
    assert (recorder.model_seen, asked) == ("project-review", [project_id])

    await generation.chat_completion_json(messages=messages)
    assert (recorder.model_seen, asked) == ("global-review", [project_id, None])


@pytest.mark.asyncio
async def test_an_explicit_model_beats_the_projects_review_model(project_id, monkeypatch):
    set_project_config(project_id, "verifier_qa_model", "project-review")
    recorder = _Recorder()
    monkeypatch.setattr(generation, "get_provider", lambda project=None: recorder)

    await generation.chat_completion_json(
        messages=[{"role": "user", "content": "go"}], project_id=project_id, model="explicit",
    )

    assert recorder.model_seen == "explicit"


def test_lessons_and_guides_are_written_on_the_projects_planner_model(project_id):
    from elstin.services.lessons import _lesson_model
    from elstin.services.project_guide import _guide_model

    set_config("planner_sdk_model", "global-planner")
    set_project_config(project_id, "planner_sdk_model", "project-planner")
    other = StateManager().create_project("Other")

    assert _lesson_model(project_id) == _guide_model(project_id) == "project-planner"
    assert _lesson_model(other) == _guide_model(other) == "global-planner"


# --- the provider's model listing ---------------------------------------------


_REAL_ASYNC_OPENAI = openai_compat.AsyncOpenAI  # before any test swaps it, so swaps do not stack


def _listing_provider(monkeypatch, handler) -> OpenAiCompatProvider:
    real_client = _REAL_ASYNC_OPENAI
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        openai_compat, "AsyncOpenAI", lambda **kwargs: real_client(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    return OpenAiCompatProvider(api_key="k", base_url="http://llm.invalid/v1")


def _listing(*ids: str) -> dict:
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 1, "owned_by": "x"} for i in ids]}


@pytest.mark.asyncio
async def test_the_adapter_lists_the_endpoints_models_sorted_and_without_repeats(monkeypatch):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json=_listing("openai/gpt-5.6-terra", "deepseek/deepseek-v4", "openai/gpt-5.6-terra"))

    provider = _listing_provider(monkeypatch, handler)

    assert await provider.list_models() == ["deepseek/deepseek-v4", "openai/gpt-5.6-terra"]
    assert requested == ["/v1/models"]


@pytest.mark.asyncio
async def test_the_adapters_listing_errors_are_the_typed_ones(monkeypatch):
    bad_key = _listing_provider(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(LlmAuthError):
        await bad_key.list_models()

    def unreachable(request: httpx.Request):
        raise httpx.ConnectError("no route to host")

    down = _listing_provider(monkeypatch, unreachable)
    with pytest.raises(LlmConnectionError):
        await down.list_models()


# --- the suggestions the picker offers ----------------------------------------


class _Provider:
    def __init__(self, result=None, *, raises: Exception | None = None, delay: float = 0.0) -> None:
        self.result, self.raises, self.delay, self.calls = result or [], raises, delay, 0

    async def list_models(self) -> list[str]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture(autouse=True)
def _fresh_model_cache():
    model_options._cache.clear()
    yield
    model_options._cache.clear()


def _use_provider(monkeypatch, provider) -> list:
    asked: list = []
    monkeypatch.setattr(model_options, "get_provider", lambda project_id=None: asked.append(project_id) or provider)
    return asked


@pytest.mark.asyncio
async def test_the_providers_own_listing_is_what_is_offered(monkeypatch):
    _use_provider(monkeypatch, _Provider(["a/one", "b/two"]))

    assert await model_options.list_model_options() == {"models": ["a/one", "b/two"], "source": "provider", "error": None}


@pytest.mark.asyncio
async def test_without_a_listing_the_models_elstin_has_prices_for_are_offered(monkeypatch):
    _use_provider(monkeypatch, _Provider(raises=LlmAuthError("bad key")))

    result = await model_options.list_model_options()

    assert result["source"] == "known" and result["models"] == known_models() and result["models"]
    assert "bad key" in result["error"]


@pytest.mark.asyncio
async def test_an_unconfigured_provider_falls_back_and_is_not_remembered(monkeypatch):
    def not_configured(project_id=None):
        raise LlmNotConfiguredError("No API key configured")

    monkeypatch.setattr(model_options, "get_provider", not_configured)
    first = await model_options.list_model_options()
    _use_provider(monkeypatch, _Provider(["a/one"]))  # the key gets saved

    assert first["source"] == "known" and "No API key" in first["error"]
    assert (await model_options.list_model_options())["models"] == ["a/one"]


@pytest.mark.asyncio
async def test_an_empty_listing_falls_back(monkeypatch):
    _use_provider(monkeypatch, _Provider([]))

    result = await model_options.list_model_options()

    assert result["source"] == "known" and "no models" in result["error"]


@pytest.mark.asyncio
async def test_a_provider_that_does_not_answer_does_not_hold_the_dialog_open(monkeypatch):
    monkeypatch.setattr(model_options, "LISTING_TIMEOUT_SECONDS", 0.05)
    _use_provider(monkeypatch, _Provider(["a/one"], delay=5))

    result = await model_options.list_model_options()

    assert result["source"] == "known" and "did not list its models" in result["error"]


@pytest.mark.asyncio
async def test_a_listing_is_cached_and_a_changed_endpoint_asks_again(monkeypatch):
    provider = _Provider(["a/one"])
    _use_provider(monkeypatch, provider)

    await model_options.list_model_options()
    await model_options.list_model_options()
    assert provider.calls == 1

    set_config("llm_base_url", "https://another.example/v1")
    await model_options.list_model_options()
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_a_failed_lookup_is_remembered_briefly_then_retried(monkeypatch):
    provider = _Provider(raises=LlmConnectionError("down"))
    _use_provider(monkeypatch, provider)
    now = [1000.0]
    monkeypatch.setattr(model_options.time, "monotonic", lambda: now[0])

    await model_options.list_model_options()
    await model_options.list_model_options()
    assert provider.calls == 1  # not hammered while it is down

    now[0] += model_options._FAILED_TTL_SECONDS + 1
    await model_options.list_model_options()
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_a_project_is_offered_its_own_providers_models_not_another_projects(project_id, monkeypatch):
    other = StateManager().create_project("Other")
    set_config("openai_api_key", "global-key")
    set_project_config(project_id, "llm_base_url", "https://project.example/v1")
    set_project_config(project_id, "openai_api_key", "project-key")
    provider = _Provider(["a/one"])
    asked = _use_provider(monkeypatch, provider)

    await model_options.list_model_options(project_id)
    await model_options.list_model_options(other)  # a different connection: not answered from the project's cache
    await model_options.list_model_options(project_id)  # the same one again: cached

    assert asked == [project_id, other]


def test_the_route_serves_the_suggestions(monkeypatch):
    _use_provider(monkeypatch, _Provider(["a/one"]))

    with _client() as client:
        response = client.get("/api/config/models")

    assert response.status_code == 200
    assert response.json() == {"models": ["a/one"], "source": "provider", "error": None}


def test_the_route_asks_the_named_projects_provider(project_id, monkeypatch):
    asked = _use_provider(monkeypatch, _Provider(["a/one"]))

    with _client() as client:
        client.get("/api/config/models", params={"project_id": project_id})

    assert asked == [project_id]
