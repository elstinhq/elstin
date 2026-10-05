"""A model endpoint that cannot be reached (forge/llm/errors.py's
LlmUnreachableError): told apart from a timeout at the adapter, waited out by the
loop for longer than a transient error, and — when that is not enough — parked and
retried by the daemon instead of failing the story.

A run failed on two connects that exceeded the OpenAI SDK's 5s default, twelve
seconds apart: a corporate proxy blip, counted as two timeouts, ending the run and
halting its epic. The sibling for rate limits and quota is tests/test_provider_limit.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import openai
import pytest

import forge.agents.loop as loop_mod
import forge.daemon.main as daemon
import forge.orchestration.graph as graph_module
from forge.agents.loop import LoopConfig, run_agent_loop, unreachable_backoff_seconds
from forge.agents.roles.dev_loop import DevLoopError
from forge.config.store import set_config
from forge.llm.base import Capabilities
from forge.llm.errors import (
    LlmConnectionError,
    LlmRateLimitError,
    LlmTimeoutError,
    LlmUnreachableError,
    ProviderLimit,
    is_retryable,
    provider_limit_of,
)
from forge.llm.openai_compat import OpenAiCompatProvider
from forge.llm.registry import get_provider
from forge.llm.types import LlmRequest, LlmResponse, TextBlock, Usage, text_message
from forge.services.provider_limit import (
    MAX_AUTO_UNREACHABLE_RETRIES,
    UNREACHABLE_REASON,
    UNREACHABLE_RETRY_SECONDS,
    USAGE_LIMIT_REASON,
    plan_parking,
)
from forge.services.run_activity import list_activity
from forge.state.manager import StateManager

# --- what is unreachable, and what is not --------------------------------------------


def test_an_unreachable_endpoint_is_retryable_but_is_not_a_timeout():
    error = LlmUnreachableError("no route to host")

    assert is_retryable(error) and isinstance(error, LlmConnectionError)
    # A timeout means a call spent its whole deadline; this one cost seconds.
    # The loop budgets them differently, so they must not be one class.
    assert not isinstance(error, LlmTimeoutError)


def test_an_unreachable_endpoint_is_a_limit_with_no_reset_time_and_a_timeout_is_not():
    limit = provider_limit_of(LlmUnreachableError("no route to host"))

    assert limit == ProviderLimit(kind="unreachable", reset_at=None, message="no route to host")
    assert provider_limit_of(LlmTimeoutError("no response within 900s")) is None
    assert provider_limit_of(LlmConnectionError("connection reset")) is None


# --- the adapter ---------------------------------------------------------------------


def _sdk_error(error_class, cause: BaseException):
    """What the SDK raises: `raise APITimeoutError(request=...) from err`."""
    import httpx2 as httpx

    error = error_class(request=httpx.Request("POST", "https://proxy.example/v1/chat/completions"))
    error.__cause__ = cause
    return error


def _http():
    import httpx2 as httpx

    return httpx


def _request() -> LlmRequest:
    return LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")])


@pytest.mark.parametrize(
    "build, expected",
    [
        # The connection was never made: unreachable.
        (lambda h: _sdk_error(openai.APITimeoutError, h.ConnectTimeout("timed out")), LlmUnreachableError),
        (lambda h: _sdk_error(openai.APIConnectionError, h.ConnectError("connection refused")), LlmUnreachableError),
        (lambda h: _sdk_error(openai.APIConnectionError, h.ProxyError("407 proxy authentication required")), LlmUnreachableError),
        # It was made and then stalled or dropped: not this class's business.
        (lambda h: _sdk_error(openai.APITimeoutError, h.ReadTimeout("no bytes for 600s")), LlmTimeoutError),
        (lambda h: _sdk_error(openai.APIConnectionError, h.ReadError("connection reset by peer")), LlmConnectionError),
    ],
)
async def test_only_a_connection_that_was_never_made_is_unreachable(monkeypatch, build, expected):
    provider = OpenAiCompatProvider(api_key="sk-test", connect_timeout_seconds=30)

    async def fails(**kwargs):
        raise build(_http())

    monkeypatch.setattr(provider._client.chat.completions, "create", fails)

    with pytest.raises(LlmConnectionError) as caught:
        await provider.complete(_request())

    assert type(caught.value) is expected


async def test_a_never_connected_cause_is_recognised_by_name_whichever_http_library_supplies_it(monkeypatch):
    """The SDK has moved between HTTP libraries (httpx, httpx2); matching on the
    class name keeps this working across that without importing either."""

    class ConnectTimeout(Exception):
        pass

    provider = OpenAiCompatProvider(api_key="sk-test")

    async def fails(**kwargs):
        raise _sdk_error(openai.APITimeoutError, ConnectTimeout("t"))

    monkeypatch.setattr(provider._client.chat.completions, "create", fails)

    with pytest.raises(LlmUnreachableError):
        await provider.complete(_request())


async def test_the_message_says_how_long_the_connect_was_given(monkeypatch):
    provider = OpenAiCompatProvider(api_key="sk-test", connect_timeout_seconds=30)

    async def fails(**kwargs):
        raise _sdk_error(openai.APITimeoutError, _http().ConnectTimeout("timed out"))

    monkeypatch.setattr(provider._client.chat.completions, "create", fails)

    with pytest.raises(LlmUnreachableError, match=r"Could not connect to the model endpoint within 30s: timed out"):
        await provider.complete(_request())


def test_the_connect_timeout_is_set_and_the_sdks_other_timeouts_are_left_alone():
    default = OpenAiCompatProvider(api_key="sk-test")._client.timeout
    longer = OpenAiCompatProvider(api_key="sk-test", connect_timeout_seconds=30)._client.timeout

    assert default.connect == 5.0  # the SDK's own, which a proxy over a VPN can miss
    assert longer.connect == 30
    assert (longer.read, longer.write, longer.pool) == (default.read, default.write, default.pool)
    # Zero or negative means "the SDK's figure", not "no timeout".
    assert OpenAiCompatProvider(api_key="sk-test", connect_timeout_seconds=0)._client.timeout.connect == 5.0


@pytest.mark.usefixtures("forge_db")
def test_the_connect_timeout_comes_from_settings_and_defaults_to_thirty_seconds():
    set_config("openai_api_key", "sk-test")
    assert get_provider()._client.timeout.connect == 30

    set_config("llm_connect_timeout_seconds", "12")
    assert get_provider()._client.timeout.connect == 12


# --- the loop ------------------------------------------------------------------------


@dataclass
class FakeProvider:
    script: list
    name: str = "fake"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list = field(default_factory=list)

    async def complete(self, request):
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _text(text: str) -> LlmResponse:
    return LlmResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5), model="fake-model")


def _config(provider, **overrides) -> LoopConfig:
    return LoopConfig(**{"provider": provider, "model": "fake-model", "system": "be helpful", **overrides})


@pytest.fixture()
def no_waiting(monkeypatch):
    """Skip the sleeps and record what each wait would have been."""
    waits: list[tuple[str, int]] = []
    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: waits.append(("transient", attempt)) or 0)
    monkeypatch.setattr(loop_mod, "unreachable_backoff_seconds", lambda failures: waits.append(("unreachable", failures)) or 0)
    return waits


async def test_a_blip_is_waited_out_and_the_turn_goes_through(no_waiting):
    provider = FakeProvider(script=[LlmUnreachableError("connect timed out"), LlmUnreachableError("connect timed out"), _text("done")])
    seen: list[tuple[int, str]] = []

    result = await run_agent_loop(
        _config(provider, on_retry=lambda attempt, error, delay: seen.append((attempt, str(error)))),
        [text_message("user", "go")],
    )

    assert result.is_error is False and result.final_text == "done"
    assert len(provider.calls) == 3
    assert no_waiting == [("unreachable", 1), ("unreachable", 2)]
    assert [attempt for attempt, _ in seen] == [1, 2]  # the feed says so: "Model call failed — retrying"


async def test_an_endpoint_that_stays_unreachable_ends_the_run_carrying_the_limit(no_waiting):
    """Five tries, where two timeouts used to end it. The run ends with the
    "unreachable" limit on its result so the story is parked, not just failed."""
    provider = FakeProvider(script=[LlmUnreachableError(f"connect {n}") for n in range(1, 8)])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True
    assert len(provider.calls) == 5
    assert "connect 5" in result.error_message
    assert result.limit == ProviderLimit(kind="unreachable", reset_at=None, message="connect 5")


async def test_unreachable_attempts_do_not_use_up_the_retries_other_errors_are_owed(no_waiting):
    provider = FakeProvider(script=[
        LlmUnreachableError("a"), LlmUnreachableError("b"), LlmUnreachableError("c"),
        LlmRateLimitError("429"), LlmRateLimitError("429"), _text("made it"),
    ])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.final_text == "made it" and len(provider.calls) == 6


async def test_a_timeout_is_still_budgeted_as_before(no_waiting):
    provider = FakeProvider(script=[LlmTimeoutError("t1"), LlmTimeoutError("t2"), _text("never reached")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True and len(provider.calls) == 2
    assert result.limit is None  # a stall is not a limit, and is not parked


def test_the_wait_grows_and_then_holds():
    assert [unreachable_backoff_seconds(n) for n in range(1, 7)] == [2.0, 5.0, 15.0, 30.0, 30.0, 30.0]
    assert unreachable_backoff_seconds(0) == 2.0


# --- parking -------------------------------------------------------------------------

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
UNREACHABLE = {"kind": "unreachable", "reset_at": None, "message": "Could not connect to the model endpoint within 30s: timed out"}


def test_an_unreachable_endpoint_is_retried_on_forges_own_schedule():
    parking = plan_parking(UNREACHABLE, retry_count=1, auto_resume=True, now=NOW)

    assert parking.retry_at == NOW + timedelta(seconds=UNREACHABLE_RETRY_SECONDS)
    assert parking.reason == UNREACHABLE_REASON
    assert parking.title == "Model endpoint unreachable — retrying automatically at 2026-10-04 09:05 UTC"
    assert "Could not connect to the model endpoint within 30s" in parking.detail


def test_a_project_that_does_not_run_on_its_own_waits_for_a_person_when_the_endpoint_is_unreachable():
    parking = plan_parking(UNREACHABLE, retry_count=0, auto_resume=False, now=NOW)

    assert parking.retry_at is None and parking.reason == UNREACHABLE_REASON
    assert "Retry when the connection is back" in parking.title


def test_a_story_that_keeps_finding_the_endpoint_down_stops_being_retried_unattended():
    assert plan_parking(UNREACHABLE, retry_count=MAX_AUTO_UNREACHABLE_RETRIES - 1, auto_resume=True, now=NOW).retry_at is not None
    stopped = plan_parking(UNREACHABLE, retry_count=MAX_AUTO_UNREACHABLE_RETRIES, auto_resume=True, now=NOW)

    assert stopped.retry_at is None and stopped.reason == UNREACHABLE_REASON
    assert "giving up on automatic retries" in stopped.title and "check the endpoint, proxy or VPN" in stopped.detail


def test_a_rate_limit_is_parked_exactly_as_before():
    parking = plan_parking({"kind": "rate", "reset_at": NOW.timestamp() + 3600, "message": "try again in 1h"}, retry_count=0, auto_resume=True)

    assert parking.reason == USAGE_LIMIT_REASON
    assert parking.retry_at == NOW + timedelta(hours=1)


# --- the daemon ----------------------------------------------------------------------


@pytest.fixture()
def board(project_id):
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", kind="dev")
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Add another", kind="dev", depends_on=["ENG-2"])
    sm.project_id = project_id
    return sm


def _park(sm: StateManager, key: str, when: datetime | None, *, reason: str = UNREACHABLE_REASON) -> None:
    sm.set_story_status(key, "failed", parked_reason=reason, failure_stage="run", retry_at=when)


@pytest.mark.usefixtures("forge_db")
def test_the_sweep_query_finds_stories_parked_on_either_reason_once_their_time_has_come(board):
    past, future = datetime.now(timezone.utc) - timedelta(minutes=1), datetime.now(timezone.utc) + timedelta(hours=1)
    board.create_story("ENG-4", epic_key="ENG-1", wave=0)
    board.create_story("ENG-5", epic_key="ENG-1", wave=0)
    _park(board, "ENG-2", past, reason=UNREACHABLE_REASON)
    _park(board, "ENG-3", past, reason=USAGE_LIMIT_REASON)
    _park(board, "ENG-4", future)  # not yet
    _park(board, "ENG-5", None)    # no retry scheduled: only a person can

    assert sorted(s["jira_key"] for s in board.list_stories_waiting_on_a_limit()) == ["ENG-2", "ENG-3"]


@pytest.mark.usefixtures("forge_db", "fake_agents")
async def test_the_sweep_retries_a_story_parked_on_an_unreachable_endpoint_and_says_why(board):
    _park(board, "ENG-2", datetime.now(timezone.utc) - timedelta(seconds=1))

    assert await daemon._resume_stories_past_their_limit(board) == ["ENG-2"]

    story = board.get_story("ENG-2")
    assert (story["retry_at"], story["parked_reason"], story["retry_count"]) == (None, None, 1)
    titles = [e["title"] for e in list_activity("ENG-2")]
    assert "Retrying automatically after the model endpoint was unreachable" in titles
    assert not any("Provider limit has lifted" in title for title in titles)


def _configure(worktrees_base_dir: str) -> None:
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", worktrees_base_dir)
    set_config("require_plan_approval", "false")


@pytest.mark.usefixtures("forge_db", "fake_agents")
async def test_an_unreachable_endpoint_parks_the_story_and_the_daemon_resumes_it_at_dev(git_repo, tmp_path, project_id, monkeypatch):
    """A failed connect's road, through the daemon's own run path and the real
    checkpointer: dev cannot reach the endpoint, the story is parked with a retry
    time of forge's own choosing, and when it comes the daemon continues at dev
    with the plan it already has. A person pressed nothing."""
    _configure(str(tmp_path / "worktrees"))
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", depends_on=[])

    planner_calls = {"n": 0}
    working_planner, working_dev = graph_module.run_planner, graph_module.run_dev

    async def counting_planner(**kwargs):
        planner_calls["n"] += 1
        return await working_planner(**kwargs)

    async def unreachable(**kwargs):
        raise DevLoopError(
            "Dev agent loop failed: Could not connect",
            limit=ProviderLimit(kind="unreachable", reset_at=None, message="Could not connect to the model endpoint within 30s"),
        )

    monkeypatch.setattr(graph_module, "run_planner", counting_planner)
    monkeypatch.setattr(graph_module, "run_dev", unreachable)

    # --- cannot connect: parked, with a time to retry at ----------------------------
    await daemon.poll_once(sm, wait=True)
    story = sm.get_story("ENG-2")
    assert (story["status"], story["failure_stage"], story["parked_reason"]) == ("failed", "run", UNREACHABLE_REASON)
    assert datetime.fromisoformat(story["retry_at"]).timestamp() == pytest.approx(time.time() + UNREACHABLE_RETRY_SECONDS, abs=30)
    assert any("Model endpoint unreachable — retrying automatically at" in e["title"] for e in list_activity("ENG-2"))

    # --- nothing happens before then --------------------------------------------------
    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["status"] == "failed" and planner_calls["n"] == 1

    # --- the time comes and the endpoint is back --------------------------------------
    monkeypatch.setattr(graph_module, "run_dev", working_dev)
    _park(sm, "ENG-2", datetime.now(timezone.utc) - timedelta(seconds=1))
    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-2")
    assert story["status"] == "ready_to_merge"
    assert (story["retry_count"], story["retry_at"], story["parked_reason"]) == (1, None, None)
    assert planner_calls["n"] == 1  # planning was not paid for a second time
