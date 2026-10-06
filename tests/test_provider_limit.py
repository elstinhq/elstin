"""A model call refused on a limit — rate or quota — is parked until it lifts, not
left failed for a person to notice.

From the bottom up: the error classification and the reset time the provider
gives, the adapter and the loop that act on it, the roles and graph that carry it,
and the daemon that parks the story and retries it. A limit whose reset time is
unknown is never retried automatically."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import openai
import pytest

import elstin.agents.loop as loop_mod
import elstin.agents.roles.dev_loop as dev_loop_module
import elstin.agents.roles.planner_loop as planner_module
import elstin.daemon.main as daemon
import elstin.orchestration.graph as graph_module
import elstin.services.generation as generation
from elstin.agents.loop import LoopConfig, run_agent_loop
from elstin.agents.roles.dev_loop import DevLoopError, run_dev
from elstin.agents.roles.planner_loop import PlannerLoopError, run_planner
from elstin.config.store import set_config, set_project_config
from elstin.llm.base import Capabilities
from elstin.llm.errors import (
    LIMIT_RESET_MARGIN_SECONDS,
    MAX_LIMIT_WAIT_SECONDS,
    LlmBadRequestError,
    LlmQuotaError,
    LlmRateLimitError,
    ProviderLimit,
    is_quota_message,
    is_retryable,
    parse_duration_seconds,
    provider_limit_of,
    retry_after_seconds,
)
from elstin.llm.openai_compat import OpenAiCompatProvider
from elstin.llm.types import LlmRequest, LlmResponse, TextBlock, Usage, text_message
from elstin.orchestration.graph import checkpointer_from_url, compile_graph
from elstin.orchestration.state import initial_state
from elstin.services.provider_limit import MAX_AUTO_LIMIT_RETRIES, USAGE_LIMIT_REASON, plan_parking
from elstin.services.run_activity import list_activity
from elstin.state.manager import StateManager


# --- what the provider says -----------------------------------------------------


@pytest.mark.parametrize(
    "text, seconds",
    [("7m12s", 432), ("250ms", 0.25), ("1h2m3.5s", 3723.5), ("20 minutes", 1200), ("1.5 hours", 5400), ("2s", 2)],
)
def test_durations(text, seconds):
    assert parse_duration_seconds(text) == pytest.approx(seconds)


def test_text_that_is_not_a_duration():
    assert parse_duration_seconds("5 stories") is None
    assert parse_duration_seconds("") is None


@pytest.mark.parametrize(
    "headers, message, expected",
    [
        ({"Retry-After": "30"}, "", 30),
        ({"retry-after-ms": "1500"}, "", 1.5),
        ({"retry-after-ms": "1500", "retry-after": "30"}, "", 1.5),  # the more exact one
        ({"x-ratelimit-reset-requests": "1s", "x-ratelimit-reset-tokens": "6m0s"}, "", 360),  # must clear both
        ({}, "Rate limit reached for gpt-5 on tokens per min. Please try again in 7m12s.", 432),
        ({}, "usage limit reached, try again in 2 hours", 7200),
        ({}, "resets in 1 hour and 30 minutes", 5400),
        ({"retry-after": "not a number or a date"}, "", None),
        ({}, "something went wrong", None),
        (None, "", None),
    ],
)
def test_when_the_provider_says_it_will_accept_calls_again(headers, message, expected):
    got = retry_after_seconds(headers, message)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_a_retry_after_http_date_is_read_as_seconds_from_now():
    soon = datetime.now(timezone.utc) + timedelta(seconds=90)
    got = retry_after_seconds({"retry-after": soon.strftime("%a, %d %b %Y %H:%M:%S GMT")})
    assert 80 <= got <= 91


@pytest.mark.parametrize(
    "message",
    [
        "You exceeded your current quota, please check your plan and billing details",
        "Error code: 402 - This request requires more credits",
        "insufficient_quota",
        "Your credit balance is too low to access the API",
        "You have reached your monthly usage limit",
    ],
)
def test_out_of_credit_wordings_are_recognised(message):
    assert is_quota_message(message)


@pytest.mark.parametrize("message", ["Rate limit reached for requests", "slow down", "the quota of tests is 5"])
def test_other_wordings_are_not(message):
    assert not is_quota_message(message)


def test_a_rate_limit_is_retryable_and_a_quota_is_not():
    assert is_retryable(LlmRateLimitError("slow down"))
    assert not is_retryable(LlmQuotaError("out of credit"))


def test_the_limit_an_error_reports():
    limit = provider_limit_of(LlmRateLimitError("slow down", retry_after_seconds=60), now=1000.0)
    assert limit == ProviderLimit(kind="rate", reset_at=1060.0 + LIMIT_RESET_MARGIN_SECONDS, message="slow down")
    assert provider_limit_of(LlmQuotaError("no credit", retry_after_seconds=10), now=0.0).kind == "quota"
    assert provider_limit_of(LlmQuotaError("no credit"), now=0.0).reset_at is None  # never guessed
    assert provider_limit_of(LlmRateLimitError("x", retry_after_seconds=MAX_LIMIT_WAIT_SECONDS + 1)).reset_at is None
    assert provider_limit_of(LlmBadRequestError("bad schema")) is None
    assert provider_limit_of(None) is None
    assert limit.as_state() == {"kind": "rate", "reset_at": limit.reset_at, "message": "slow down"}


# --- the adapter -----------------------------------------------------------------


def _http_response(status: int, headers: dict | None = None):
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return httpx.Response(status, request=request, headers=headers or {})


async def _call_failing_with(monkeypatch, error: Exception):
    provider = OpenAiCompatProvider(api_key="sk-test")

    async def fake_create(**kwargs):
        raise error

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)
    return await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))


async def test_a_429_carries_the_wait_the_provider_asked_for(monkeypatch):
    error = openai.RateLimitError("slow down", response=_http_response(429, {"retry-after": "3600"}), body=None)
    with pytest.raises(LlmRateLimitError) as caught:
        await _call_failing_with(monkeypatch, error)
    assert caught.value.retry_after_seconds == 3600


async def test_an_openai_style_reset_header_is_read(monkeypatch):
    error = openai.RateLimitError(
        "slow down", response=_http_response(429, {"x-ratelimit-reset-tokens": "6m0s"}), body=None,
    )
    with pytest.raises(LlmRateLimitError) as caught:
        await _call_failing_with(monkeypatch, error)
    assert caught.value.retry_after_seconds == 360


async def test_a_429_coded_insufficient_quota_is_a_quota_not_a_rate_limit(monkeypatch):
    error = openai.RateLimitError(
        "You exceeded your current quota", response=_http_response(429),
        body={"message": "You exceeded your current quota", "code": "insufficient_quota"},
    )
    with pytest.raises(LlmQuotaError):
        await _call_failing_with(monkeypatch, error)


async def test_a_429_whose_message_says_out_of_credit_is_a_quota(monkeypatch):
    error = openai.RateLimitError("Your credit balance is too low", response=_http_response(429), body=None)
    with pytest.raises(LlmQuotaError):
        await _call_failing_with(monkeypatch, error)


async def test_a_402_from_a_gateway_is_a_quota(monkeypatch):
    error = openai.APIStatusError("This request requires more credits", response=_http_response(402), body=None)
    with pytest.raises(LlmQuotaError):
        await _call_failing_with(monkeypatch, error)


async def test_an_ordinary_429_is_still_a_rate_limit_with_no_wait(monkeypatch):
    error = openai.RateLimitError("slow down", response=_http_response(429), body=None)
    with pytest.raises(LlmRateLimitError) as caught:
        await _call_failing_with(monkeypatch, error)
    assert caught.value.retry_after_seconds is None and not isinstance(caught.value, LlmQuotaError)


# --- the loop ---------------------------------------------------------------------


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
    return LlmResponse(
        content=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5), model="gpt-5.1",
    )


def _config(provider) -> LoopConfig:
    return LoopConfig(provider=provider, model="fake-model", system="be helpful")


async def test_a_short_wait_the_provider_asked_for_is_slept_in_place_and_the_call_retried(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(loop_mod.asyncio, "sleep", fake_sleep)
    provider = FakeProvider(script=[LlmRateLimitError("slow down", retry_after_seconds=20), _text("ok now")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is False and result.limit is None
    assert len(provider.calls) == 2
    assert slept == [21.0]  # the provider's own figure plus a second, not our 2-second guess


async def test_a_long_wait_ends_the_loop_at_once_with_the_limit_on_its_result(monkeypatch):
    async def never_sleeps(seconds):
        raise AssertionError("must not sit through an hour")

    monkeypatch.setattr(loop_mod.asyncio, "sleep", never_sleeps)
    provider = FakeProvider(script=[LlmRateLimitError("daily limit", retry_after_seconds=3600), _text("never")])
    before = time.time()

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True and result.terminal_reason == "error"
    assert len(provider.calls) == 1  # no three attempts against a window that is shut
    assert result.limit.kind == "rate"
    assert before + 3600 <= result.limit.reset_at <= time.time() + 3600 + LIMIT_RESET_MARGIN_SECONDS + 1


async def test_a_quota_is_not_retried_and_says_it_has_no_reset_time():
    provider = FakeProvider(script=[LlmQuotaError("You exceeded your current quota"), _text("never")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert len(provider.calls) == 1
    assert result.limit == ProviderLimit(kind="quota", reset_at=None, message="You exceeded your current quota")


async def test_a_failure_that_is_not_a_limit_carries_none():
    result = await run_agent_loop(_config(FakeProvider(script=[LlmBadRequestError("bad schema")])), [text_message("user", "go")])
    assert result.is_error and result.limit is None


# --- the roles --------------------------------------------------------------------


@pytest.mark.usefixtures("elstin_db")
async def test_dev_raises_an_error_that_carries_the_limit(git_repo, monkeypatch):
    set_config("openai_api_key", "sk-test")
    provider = FakeProvider(script=[LlmRateLimitError("hourly limit", retry_after_seconds=1800)])
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *a, **k: provider)

    with pytest.raises(DevLoopError) as caught:
        await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)

    assert caught.value.limit.kind == "rate" and caught.value.limit.reset_at is not None
    assert len(provider.calls) == 1


@pytest.mark.usefixtures("elstin_db")
async def test_the_planner_raises_an_error_that_carries_the_limit_and_is_not_retried(git_repo, monkeypatch):
    set_config("openai_api_key", "sk-test")
    set_config("planner_explorers_enabled", "false")
    provider = FakeProvider(script=[LlmQuotaError("out of credit")])
    monkeypatch.setattr(planner_module, "get_provider", lambda *a, **k: provider)

    with pytest.raises(PlannerLoopError) as caught:
        await run_planner(user_request="Add a widget", project_path=git_repo)

    assert caught.value.limit.kind == "quota" and caught.value.limit.reset_at is None
    assert len(provider.calls) == 1


@pytest.mark.usefixtures("elstin_db")
async def test_a_single_shot_review_failure_carries_the_limit(monkeypatch):
    provider = FakeProvider(script=[LlmRateLimitError("slow down", retry_after_seconds=600)])
    monkeypatch.setattr(generation, "get_provider", lambda *a, **k: provider)

    with pytest.raises(generation.GenerationError) as caught:
        await generation.chat_completion_json(messages=[{"role": "user", "content": "review"}])

    assert caught.value.limit.kind == "rate" and caught.value.limit.reset_at is not None


# --- the graph --------------------------------------------------------------------

LIMIT = ProviderLimit(kind="rate", reset_at=time.time() + 3600, message="daily limit")


@pytest.mark.usefixtures("elstin_db", "fake_agents")
@pytest.mark.parametrize("node", ["planner", "dev", "verifier", "qa"])
async def test_a_run_that_ends_on_a_limit_says_so_in_its_final_state(node, postgres_url, monkeypatch):
    errors = {
        "planner": ("run_planner", PlannerLoopError), "dev": ("run_dev", DevLoopError),
        "verifier": ("run_verifier", generation.GenerationError), "qa": ("run_epic_qa", generation.GenerationError),
    }
    attr, error_type = errors[node]

    async def refused(**kwargs):
        raise error_type("the provider refused", limit=LIMIT)

    monkeypatch.setattr(graph_module, attr, refused)
    state = initial_state("ENG-2", "Add a widget", "/repo")
    state["worktree_path"] = "/tmp/anywhere"
    if node == "qa":
        state["story_kind"] = "qa"
        state["qa_context"] = {"epic": {"jira_key": "ENG-1"}}

    async with checkpointer_from_url(postgres_url) as checkpointer:
        final = await compile_graph(checkpointer, require_plan_approval=False).ainvoke(
            state, {"configurable": {"thread_id": f"limit-{node}"}},
        )

    assert final["status"] == "failed"
    assert final["provider_limit"] == LIMIT.as_state()


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_a_failure_that_is_not_a_limit_leaves_the_field_empty(postgres_url, monkeypatch):
    async def refused(**kwargs):
        raise DevLoopError("bad schema")

    monkeypatch.setattr(graph_module, "run_dev", refused)
    state = initial_state("ENG-2", "Add a widget", "/repo")
    state["worktree_path"] = "/tmp/anywhere"
    async with checkpointer_from_url(postgres_url) as checkpointer:
        final = await compile_graph(checkpointer, require_plan_approval=False).ainvoke(
            state, {"configurable": {"thread_id": "no-limit"}},
        )
    assert final["status"] == "failed" and final.get("provider_limit") is None


# --- what to do about it ------------------------------------------------------------

RESET = datetime(2026, 10, 2, 14, 5, tzinfo=timezone.utc)
KNOWN = {"kind": "rate", "reset_at": RESET.timestamp(), "message": "Please try again in 1h"}


def test_a_known_reset_means_an_automatic_retry_then():
    parking = plan_parking(KNOWN, retry_count=0, auto_resume=True)
    assert parking.retry_at == RESET
    assert parking.title == "Provider rate limit reached — retrying automatically at 2026-10-02 14:05 UTC"
    assert "Please try again in 1h" in parking.detail


def test_an_unknown_reset_is_never_guessed():
    quota = plan_parking({"kind": "quota", "reset_at": None, "message": "out of credit"}, retry_count=0, auto_resume=True)
    rate = plan_parking({"kind": "rate", "reset_at": None, "message": ""}, retry_count=0, auto_resume=True)
    assert quota.retry_at is None and rate.retry_at is None
    assert "add credit, then Retry" in quota.title
    assert "Retry later" in rate.title


def test_a_project_that_does_not_run_on_its_own_waits_for_a_person():
    parking = plan_parking(KNOWN, retry_count=0, auto_resume=False)
    assert parking.retry_at is None
    assert "lifts at 2026-10-02 14:05 UTC; Retry then" in parking.title


def test_a_story_retried_too_often_stops_being_retried_unattended():
    assert plan_parking(KNOWN, retry_count=MAX_AUTO_LIMIT_RETRIES - 1, auto_resume=True).retry_at == RESET
    stopped = plan_parking(KNOWN, retry_count=MAX_AUTO_LIMIT_RETRIES, auto_resume=True)
    assert stopped.retry_at is None and "giving up on automatic retries" in stopped.title


# --- the daemon -----------------------------------------------------------------------

@pytest.fixture()
def board(project_id):
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", kind="dev")
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Add another", kind="dev", depends_on=["ENG-2"])
    sm.project_id = project_id
    return sm


def _park(sm: StateManager, key: str, when: datetime | None, *, reason: str = USAGE_LIMIT_REASON, stage: str = "run") -> None:
    sm.set_story_status(key, "failed", parked_reason=reason, failure_stage=stage, retry_at=when)


@pytest.mark.usefixtures("elstin_db")
def test_only_a_failed_run_parked_on_a_limit_whose_time_has_come_is_due(board):
    past, future = datetime.now(timezone.utc) - timedelta(minutes=1), datetime.now(timezone.utc) + timedelta(hours=1)
    board.create_story("ENG-4", epic_key="ENG-1", wave=0)
    board.create_story("ENG-5", epic_key="ENG-1", wave=0)
    board.create_story("ENG-6", epic_key="ENG-1", wave=0)
    _park(board, "ENG-2", past)
    _park(board, "ENG-3", future)                      # not yet
    _park(board, "ENG-4", None)                        # the provider did not say when
    _park(board, "ENG-5", past, reason="cancelled")    # failed for another reason
    _park(board, "ENG-6", past, stage="merge")         # landing, not a run

    assert [s["jira_key"] for s in board.list_stories_waiting_on_a_limit()] == ["ENG-2"]


@pytest.mark.usefixtures("elstin_db")
def test_a_status_change_clears_the_retry_time(board):
    _park(board, "ENG-2", datetime.now(timezone.utc) + timedelta(hours=1))
    assert board.get_story("ENG-2")["retry_at"] is not None
    board.set_story_status("ENG-2", "pending")
    assert board.get_story("ENG-2")["retry_at"] is None


@pytest.mark.usefixtures("elstin_db")
def test_a_persons_retry_clears_the_schedule_too(board):
    _park(board, "ENG-2", datetime.now(timezone.utc) + timedelta(hours=1))
    board.retry_story("ENG-2")
    story = board.get_story("ENG-2")
    assert (story["status"], story["retry_at"], story["parked_reason"]) == ("pending", None, None)


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_the_sweep_retries_a_story_whose_limit_has_lifted(board):
    board.set_epic_phase("ENG-1", "failed")
    _park(board, "ENG-2", datetime.now(timezone.utc) - timedelta(seconds=1))

    assert await daemon._resume_stories_past_their_limit(board) == ["ENG-2"]

    story = board.get_story("ENG-2")
    assert story["status"] == "pending"  # no checkpoint to resume from, so planned again
    assert (story["retry_at"], story["parked_reason"], story["retry_count"]) == (None, None, 1)
    assert board.get_epic("ENG-1")["phase"] == "implementing"
    titles = [e["title"] for e in list_activity("ENG-2")]
    assert "Provider limit has lifted — retrying automatically" in titles
    assert await daemon._resume_stories_past_their_limit(board) == []  # nothing left to do


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_the_sweep_leaves_a_story_whose_time_has_not_come(board):
    _park(board, "ENG-2", datetime.now(timezone.utc) + timedelta(hours=1))
    assert await daemon._resume_stories_past_their_limit(board) == []
    assert board.get_story("ENG-2")["status"] == "failed"


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_the_sweep_leaves_a_manual_projects_story_to_its_person(board):
    set_project_config(board.project_id, "auto_run_enabled", "false")
    _park(board, "ENG-2", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await daemon._resume_stories_past_their_limit(board) == []
    assert board.get_story("ENG-2")["status"] == "failed"


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_the_sweep_leaves_a_story_with_a_run_in_flight(board):
    _park(board, "ENG-2", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert daemon.claim_run("ENG-2")
    try:
        assert await daemon._resume_stories_past_their_limit(board) == []
    finally:
        daemon.release_run("ENG-2")


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_a_checkpoint_that_cannot_be_read_still_retries_with_a_fresh_plan(board, monkeypatch):
    async def broken(sm, epic, story):
        raise RuntimeError("the checkpoint store is unreachable")

    monkeypatch.setattr(daemon, "arm_resume_after_failure", broken)
    _park(board, "ENG-2", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await daemon._resume_stories_past_their_limit(board) == ["ENG-2"]
    assert board.get_story("ENG-2")["status"] == "pending"


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_poll_once_runs_the_sweep_before_the_epics(board, monkeypatch):
    calls: list[str] = []

    async def fake_sweep(sm):
        calls.append("sweep")
        return []

    monkeypatch.setattr(daemon, "_resume_stories_past_their_limit", fake_sweep)
    await daemon.poll_once(board, wait=True)
    assert calls == ["sweep"]

    async def broken(sm):
        raise RuntimeError("boom")

    monkeypatch.setattr(daemon, "_resume_stories_past_their_limit", broken)
    await daemon.poll_once(board, wait=True)  # a failing sweep never stalls the poll


def _configure(worktrees_base_dir: str) -> None:
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", worktrees_base_dir)
    set_config("require_plan_approval", "false")


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_a_limit_parks_the_story_and_the_daemon_resumes_it_at_dev_once_it_lifts(
    git_repo, tmp_path, project_id, monkeypatch,
):
    """The whole road, through the daemon's own run path and the real checkpointer:
    dev is refused on a daily limit, the story ends failed with the time to retry
    and an activity line that says so; when that time has passed the daemon retries
    it on its own — continuing at dev with the plan it already has — and it
    finishes. A person pressed nothing."""
    _configure(str(tmp_path / "worktrees"))
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", depends_on=[])

    planner_calls = {"n": 0}
    working_planner, working_dev = graph_module.run_planner, graph_module.run_dev
    reset = time.time() + 3600

    async def counting_planner(**kwargs):
        planner_calls["n"] += 1
        return await working_planner(**kwargs)

    async def refused(**kwargs):
        raise DevLoopError("daily limit", limit=ProviderLimit(kind="rate", reset_at=reset, message="daily limit"))

    monkeypatch.setattr(graph_module, "run_planner", counting_planner)
    monkeypatch.setattr(graph_module, "run_dev", refused)

    # --- refused: parked, with the time to retry --------------------------------
    await daemon.poll_once(sm, wait=True)
    story = sm.get_story("ENG-2")
    assert (story["status"], story["failure_stage"], story["parked_reason"]) == ("failed", "run", USAGE_LIMIT_REASON)
    assert datetime.fromisoformat(story["retry_at"]).timestamp() == pytest.approx(reset, abs=2)
    assert any("retrying automatically at" in e["title"] for e in list_activity("ENG-2"))

    # --- nothing happens while the limit holds ------------------------------------
    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["status"] == "failed"
    assert planner_calls["n"] == 1

    # --- the limit lifts: the daemon retries on its own, at dev ---------------------
    monkeypatch.setattr(graph_module, "run_dev", working_dev)
    sm.set_story_status(
        "ENG-2", "failed", parked_reason=USAGE_LIMIT_REASON, failure_stage="run",
        retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-2")
    assert story["status"] == "ready_to_merge"
    assert (story["retry_count"], story["retry_at"], story["parked_reason"]) == (1, None, None)
    assert planner_calls["n"] == 1  # planning was not paid for a second time
    titles = [e["title"] for e in list_activity("ENG-2")]
    assert "Provider limit has lifted — retrying automatically: continuing at development with the earlier plan" in titles


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_a_quota_with_no_reset_time_parks_the_story_with_no_retry_scheduled(git_repo, tmp_path, project_id, monkeypatch):
    _configure(str(tmp_path / "worktrees"))
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget", depends_on=[])

    async def out_of_credit(**kwargs):
        raise DevLoopError("no credit", limit=ProviderLimit(kind="quota", reset_at=None, message="no credit"))

    monkeypatch.setattr(graph_module, "run_dev", out_of_credit)
    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-2")
    assert (story["status"], story["parked_reason"], story["retry_at"]) == ("failed", USAGE_LIMIT_REASON, None)
    assert any("add credit, then Retry" in e["title"] for e in list_activity("ENG-2"))
    assert await daemon._resume_stories_past_their_limit(sm) == []  # never guessed at
