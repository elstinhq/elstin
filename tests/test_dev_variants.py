"""Competing dev attempts (forge/agents/roles/dev_variants.py): when the
deterministic gate has failed repeatedly, several fresh attempts run side by side
in scratch worktrees and the gate picks the best.

Real git for the worktrees and for adopting a winner; the model (`run_dev`) and the
gate are scripted, since what is under test is the choosing, the isolation, the
adoption and the cleanup."""
from __future__ import annotations

import asyncio
import os
import subprocess
import uuid

import pytest

import forge.agents.roles.dev_variants as variants
import forge.orchestration.graph as graph_module
from forge.agents.roles.dev_variants import (
    APPROACH_HINTS,
    Attempt,
    adopt_changes,
    choose,
    failures_before_variants,
    not_eligible_reason,
    rank_key,
    run_dev_variants,
    scoreboard,
    variant_count,
)
from forge.config.store import set_config, set_project_config
from forge.orchestration.graph import checkpointer_from_url, compile_graph
from forge.orchestration.state import initial_state
from forge.services.run_activity import list_activity
from forge.state.manager import StateManager
from forge.tools.worktree import create_scratch_worktree, create_worktree, discard_worktree, run_git, variant_worktree_path

KEY = "ENG-2"


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def _write(root: str, relative: str, text: str) -> None:
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def _read(root: str, relative: str) -> str | None:
    try:
        with open(os.path.join(root, relative)) as handle:
            return handle.read()
    except FileNotFoundError:
        return None


# --- settings ---------------------------------------------------------------------


@pytest.mark.usefixtures("forge_db")
def test_competing_attempts_are_off_by_default_and_clamped():
    assert variant_count(None) == 1
    assert failures_before_variants(None) == 2
    set_config("dev_variants", "3")
    assert variant_count(None) == 3
    set_config("dev_variants", "99")
    assert variant_count(None) == variants.MAX_VARIANTS
    set_config("dev_variants", "-2")
    assert variant_count(None) == 1
    set_config("dev_variants", "lots")
    assert variant_count(None) == 1
    set_config("dev_variants_after_gate_failures", "0")
    assert failures_before_variants(None) == 1
    set_config("dev_variants_after_gate_failures", "soon")
    assert failures_before_variants(None) == 2


@pytest.mark.usefixtures("forge_db")
def test_a_project_can_opt_in_for_itself(project_id):
    assert variant_count(project_id) == 1
    set_project_config(project_id, "dev_variants", "2")
    assert variant_count(project_id) == 2
    assert variant_count(None) == 1  # another project is unaffected


# --- when they may run ---------------------------------------------------------------

ELIGIBLE = {"started_clean": True, "plan": {"files_to_modify": [{"path": "src/a.py"}]}, "default_repo_label": "api"}


def test_a_clean_single_repo_dev_run_may_compete():
    assert not_eligible_reason(ELIGIBLE) is None


@pytest.mark.parametrize(
    "change, why",
    [
        ({"story_kind": "qa"}, "QA story"),
        ({"variants_done": True}, "already tried"),
        ({"started_clean": False}, "uncommitted work from an earlier run"),
        ({"started_clean": None}, "uncommitted work from an earlier run"),  # a checkpoint from before the flag
        ({"integration_paths": ["src/a.py"]}, "land on the epic branch"),
        ({"plan": {"files_to_modify": [{"path": "src/a.py"}, {"path": "@ui/src/b.ts"}]}}, "more than one repo"),
    ],
)
def test_it_must_not_replace_a_worktree_that_holds_anything_else(change, why):
    reason = not_eligible_reason({**ELIGIBLE, **change})
    assert reason and why in reason


# --- choosing -------------------------------------------------------------------------


def _attempt(index, *, passed=None, blockers=0, files=1, error=None, judged=True) -> Attempt:
    a = Attempt(index, f"/wt/{index}", error=error)
    if passed is not None or judged:
        a.result = {"file_changes": [{"path": f"f{n}"} for n in range(files)]}
        a.gate = {"passed": bool(passed), "required_fixes": [f"fix {n}" for n in range(blockers)]}
    return a


def test_a_pass_beats_a_fail_whatever_the_size():
    assert choose([_attempt(1, passed=False, blockers=1, files=1), _attempt(2, passed=True, files=9)]).index == 2


def test_among_failures_fewer_blocking_issues_win():
    assert choose([_attempt(1, passed=False, blockers=3), _attempt(2, passed=False, blockers=1)]).index == 2


def test_among_passes_the_smaller_change_wins_then_the_lower_number():
    assert choose([_attempt(1, passed=True, files=4), _attempt(2, passed=True, files=2)]).index == 2
    assert choose([_attempt(1, passed=True, files=2), _attempt(2, passed=True, files=2)]).index == 1


def test_an_attempt_the_gate_never_saw_is_not_a_candidate():
    unjudged = Attempt(1, "/wt/1", error="the model failed")
    assert choose([unjudged]) is None
    assert choose([unjudged, _attempt(2, passed=False, blockers=5)]).index == 2


def test_issues_stand_in_for_required_fixes_when_there_are_none():
    a = Attempt(1, "/wt/1", result={"file_changes": []}, gate={"passed": False, "issues": [{}, {}]})
    assert rank_key(a)[1] == 2


def test_the_scoreboard_says_what_each_attempt_did():
    attempts = [
        _attempt(1, passed=True, files=2), _attempt(2, passed=False, blockers=3, files=1),
        Attempt(3, "/wt/3", error="boom"), Attempt(4, "/wt/4", result={"file_changes": []}),
    ]
    board = scoreboard(attempts, attempts[0])
    assert board.splitlines() == [
        "variant 1 (chosen): passed the gate, 2 file(s)",
        "variant 2: failed the gate with 3 issue(s), 1 file(s)",
        "variant 3: failed: boom",
        "variant 4: not judged",
    ]


# --- worktrees and adoption (real git) ---------------------------------------------------


@pytest.fixture()
def story(git_repo, tmp_path):
    """The repo, with a story worktree: the real one the winner is adopted into."""
    base = str(tmp_path / "worktrees")
    made = create_worktree(git_repo, story_key=KEY, summary="story", worktrees_base_dir=base)
    return type("Story", (), {"repo": git_repo, "base": base, "path": made["worktree_path"], "branch": made["branch"]})


def test_a_scratch_worktree_is_detached_at_the_commit_and_leaves_no_branch(story):
    head = _git(story.path, "rev-parse", "HEAD").strip()
    path = variant_worktree_path(story.base, KEY, 1)
    branches = set(_git(story.repo, "for-each-ref", "refs/heads").split("\n"))

    created = create_scratch_worktree(story.repo, path, head)

    assert created["worktree_path"] == path and os.path.isdir(path)
    assert _git(path, "rev-parse", "HEAD").strip() == head
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"  # detached
    assert set(_git(story.repo, "for-each-ref", "refs/heads").split("\n")) == branches
    discard_worktree(story.repo, path, None)
    assert not os.path.exists(path)
    assert path not in _git(story.repo, "worktree", "list", "--porcelain")


def test_a_scratch_worktree_path_is_under_variants_and_per_story(story):
    path = variant_worktree_path(story.base, "ENG-2", 3)
    assert path.endswith(os.path.join("_variants", "eng-2-v3"))


def test_adopting_makes_the_destination_exactly_the_winner(story, tmp_path):
    head = _git(story.path, "rev-parse", "HEAD").strip()
    winner = create_scratch_worktree(story.repo, variant_worktree_path(story.base, KEY, 1), head)["worktree_path"]
    _write(story.path, ".gitignore", "node_modules/\n")
    _git(story.path, "add", ".gitignore")
    _git(story.path, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "ignore")
    # both worktrees at the same commit again
    head = _git(story.path, "rev-parse", "HEAD").strip()
    discard_worktree(story.repo, winner, None)
    winner = create_scratch_worktree(story.repo, variant_worktree_path(story.base, KEY, 1), head)["worktree_path"]

    # The failed attempt, in the story worktree: a change, a new file, a deletion, installed deps.
    _write(story.path, "README.md", "the failed attempt's edit\n")
    _write(story.path, "stale/new.py", "stale\n")
    _write(story.path, "node_modules/dep/index.js", "installed dependency\n")
    # The winner: a different edit, a new nested file, and README's sibling deleted.
    _write(winner, "README.md", "the winner's edit\n")
    _write(winner, "src/deep/feature.py", "def feature(): ...\n")
    _write(winner, "node_modules/other/index.js", "ignored on this side too\n")

    changed = adopt_changes(winner, story.path)

    assert sorted(changed) == ["README.md", "src/deep/feature.py"]
    assert _read(story.path, "README.md") == "the winner's edit\n"
    assert _read(story.path, "src/deep/feature.py") == "def feature(): ...\n"
    assert _read(story.path, "stale/new.py") is None  # the failed attempt's file is gone
    assert _read(story.path, "node_modules/dep/index.js") == "installed dependency\n"  # ignored files stay
    assert _read(story.path, "node_modules/other/index.js") is None  # and are not copied
    assert _git(story.path, "status", "--porcelain").split() == ["M", "README.md", "??", "src/"]
    discard_worktree(story.repo, winner, None)


def test_adopting_carries_a_deletion(story):
    _write(story.path, "old.txt", "to be removed\n")
    _git(story.path, "add", "old.txt")
    _git(story.path, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "add old")
    head = _git(story.path, "rev-parse", "HEAD").strip()
    winner = create_scratch_worktree(story.repo, variant_worktree_path(story.base, KEY, 1), head)["worktree_path"]
    os.remove(os.path.join(winner, "old.txt"))

    assert adopt_changes(winner, story.path) == ["old.txt"]

    assert not os.path.exists(os.path.join(story.path, "old.txt"))
    discard_worktree(story.repo, winner, None)


def test_a_path_that_would_escape_the_worktree_is_refused(story, monkeypatch):
    head = _git(story.path, "rev-parse", "HEAD").strip()
    winner = create_scratch_worktree(story.repo, variant_worktree_path(story.base, KEY, 1), head)["worktree_path"]
    real = variants.run_git

    def forged(path, *args, **kwargs):
        if args[:2] == ("diff", "--cached"):
            return subprocess.CompletedProcess(args, 0, stdout="M\0../../escape.txt\0", stderr="")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(variants, "run_git", forged)
    with pytest.raises(Exception, match="outside the worktree"):
        adopt_changes(winner, story.path)
    discard_worktree(story.repo, winner, None)


@pytest.mark.usefixtures("forge_db")
def test_a_label_prefixes_what_one_attempt_writes_and_nothing_else(project_id):
    from forge.services.run_activity import activity_label, append_activity

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story(KEY, epic_key="ENG-1", wave=0)
    with activity_label("variant 2"):
        append_activity(KEY, type="status", phase="dev", title="Development started")
    append_activity(KEY, type="status", phase="dev", title="Development started")
    assert [e["title"] for e in list_activity(KEY)] == ["[variant 2] Development started", "Development started"]


# --- the whole thing --------------------------------------------------------------------


class Script:
    """What the scripted model writes in each attempt, and what the scripted gate says."""

    def __init__(self, story, outcomes: dict[int, str]) -> None:
        self.story = story
        self.outcomes = outcomes  # attempt index -> "good" | "bad" | "boom" | "bigger-good"
        self.dev_calls: list[dict] = []
        self.gate_calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def index_of(self, path: str) -> int:
        return int(path.rsplit("-v", 1)[1])

    async def run_dev(self, **kwargs):
        self.dev_calls.append(kwargs)
        index = self.index_of(kwargs["worktree_path"])
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.05)  # long enough for the others to start: they run side by side
            from forge.services.run_activity import append_activity

            append_activity(kwargs["run_id"], type="status", phase="dev", title="Development started")
            outcome = self.outcomes[index]
            if outcome == "boom":
                raise RuntimeError("the model failed")
            _write(kwargs["worktree_path"], "feature.py", f"# attempt {index}: {outcome}\n")
            if outcome == "bigger-good":
                _write(kwargs["worktree_path"], "extra.py", "# a second file\n")
            return {
                "file_changes": [{"path": "feature.py"}] + ([{"path": "extra.py"}] if outcome == "bigger-good" else []),
                "summary": f"attempt {index}", "truncated": False, "messages": [{"role": "dev", "content": f"attempt {index}"}],
                "dev_history": [{"attempt": index}], "dev_progress": [], "declared_unchanged": {},
                "story_worktrees": {"api": {"repo_path": self.story.repo, "worktree_path": kwargs["worktree_path"], "branch": ""}},
                "dev_build_runner": {},
            }
        finally:
            self.in_flight -= 1

    async def gate(self, **kwargs):
        self.gate_calls.append(kwargs["worktree_path"])
        text = _read(kwargs["worktree_path"], "feature.py") or ""
        passed = "good" in text
        return {"passed": passed, "issues": [] if passed else [{"reason": "bad"}], "required_fixes": [] if passed else ["fix it"]}


@pytest.fixture()
def arena(story, project_id, monkeypatch):
    set_config("worktrees_base_dir", story.base)
    set_config("data_dir", str(os.path.dirname(story.base)))
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story(KEY, epic_key="ENG-1", wave=0)

    def build(outcomes: dict[int, str]) -> Script:
        script = Script(story, outcomes)
        monkeypatch.setattr(variants, "run_dev", script.run_dev)
        monkeypatch.setattr(variants, "run_deterministic_gate", script.gate)
        return script

    return type("Arena", (), {"build": staticmethod(build), "story": story})


async def _compete(story, *, count=3, **overrides):
    _write(story.path, "feature.py", "# the failed attempt: bad\n")  # what the story worktree holds now
    return await run_dev_variants(
        count=count, plan={"files_to_modify": [{"path": "feature.py"}]}, context_bundle={"project_context": "ctx"},
        worktree_path=story.path, project_path=story.repo, run_id=KEY, default_label="api",
        story_worktrees={"api": {"repo_path": story.repo, "worktree_path": story.path, "branch": story.branch}},
        **overrides,
    )


@pytest.mark.usefixtures("forge_db")
async def test_the_attempt_that_passes_the_gate_is_adopted_and_everything_else_is_cleaned_up(arena):
    script = arena.build({1: "bad", 2: "good", 3: "bad"})

    result = await _compete(arena.story)

    # adopted: the story worktree now holds the winner's file and not the failed attempt's
    assert _read(arena.story.path, "feature.py") == "# attempt 2: good\n"
    assert result["dev_history"] == [{"attempt": 2}]
    assert result["story_worktrees"]["api"]["worktree_path"] == arena.story.path  # not the scratch path
    assert [(v["index"], v["passed"], v["chosen"]) for v in result["variants"]] == [(1, False, False), (2, True, True), (3, False, False)]
    # side by side, and judged one at a time
    assert script.max_in_flight == 3
    assert sorted(script.gate_calls) == sorted(variant_worktree_path(arena.story.base, KEY, i) for i in (1, 2, 3))
    # cleaned up: no scratch worktrees on disk or in git, no baselines
    for i in (1, 2, 3):
        assert not os.path.exists(variant_worktree_path(arena.story.base, KEY, i))
    assert "_variants" not in _git(arena.story.repo, "worktree", "list", "--porcelain")
    assert not [d for d in os.listdir(os.path.dirname(arena.story.base)) if "~v" in d]


@pytest.mark.usefixtures("forge_db")
async def test_each_attempt_is_isolated_and_gets_a_share_of_the_budget(arena):
    script = arena.build({1: "good", 2: "good"})

    await _compete(arena.story, count=2)

    calls = sorted(script.dev_calls, key=lambda c: c["worktree_path"])
    assert [c["baseline_key"] for c in calls] == [f"{KEY}~v1", f"{KEY}~v2"]
    assert all(c["budget_share"] == 0.5 for c in calls)
    assert all(c["read_guidance"] is False for c in calls)
    assert all(c["run_id"] == KEY for c in calls)  # their spend counts against the story
    # each writes to its own scratch worktree, which is where dev is told the story's repo lives
    for c in calls:
        assert c["story_worktrees"]["api"]["worktree_path"] == c["worktree_path"]


@pytest.mark.usefixtures("forge_db")
async def test_attempts_take_different_approaches(arena):
    script = arena.build({1: "good", 2: "good", 3: "good"})

    await _compete(arena.story)

    contexts = [c["context_bundle"]["project_context"] for c in sorted(script.dev_calls, key=lambda c: c["worktree_path"])]
    assert contexts[0] == "ctx"  # the first is the plain plan
    assert contexts[1].endswith(APPROACH_HINTS[1]) and contexts[2].endswith(APPROACH_HINTS[2])
    assert len(set(contexts)) == 3


@pytest.mark.usefixtures("forge_db")
async def test_every_attempt_writes_to_the_feed_under_its_own_label(arena):
    arena.build({1: "good", 2: "good"})

    await _compete(arena.story, count=2)

    titles = [e["title"] for e in list_activity(KEY)]
    assert "[variant 1] Development started" in titles and "[variant 2] Development started" in titles
    assert any(t.startswith("Adopted variant") for t in titles)
    adopted = next(e for e in list_activity(KEY) if e["title"].startswith("Adopted variant"))
    assert "variant 1 (chosen)" in adopted["detail"]


@pytest.mark.usefixtures("forge_db")
async def test_when_every_attempt_passes_the_smaller_change_is_adopted(arena):
    arena.build({1: "bigger-good", 2: "good"})
    result = await _compete(arena.story, count=2)
    assert [v["chosen"] for v in result["variants"]] == [False, True]
    assert _read(arena.story.path, "extra.py") is None


@pytest.mark.usefixtures("forge_db")
async def test_an_attempt_that_errors_is_a_result_not_a_failure_of_the_others(arena):
    arena.build({1: "boom", 2: "good", 3: "boom"})
    result = await _compete(arena.story)
    assert [v["error"] for v in result["variants"]] == ["the model failed", None, "the model failed"]
    assert _read(arena.story.path, "feature.py") == "# attempt 2: good\n"


@pytest.mark.usefixtures("forge_db")
async def test_with_nothing_to_adopt_the_story_worktree_is_untouched(arena):
    arena.build({1: "boom", 2: "boom"})

    assert await _compete(arena.story, count=2) is None

    assert _read(arena.story.path, "feature.py") == "# the failed attempt: bad\n"
    assert not os.path.exists(variant_worktree_path(arena.story.base, KEY, 1))
    assert any("none produced a result" in e["title"] for e in list_activity(KEY))


@pytest.mark.usefixtures("forge_db")
async def test_fewer_than_two_usable_scratch_worktrees_is_not_a_comparison(arena, monkeypatch):
    arena.build({1: "good", 2: "good", 3: "good"})
    real = variants.create_scratch_worktree

    def flaky(project_path, path, ref):
        if path.endswith("-v2") or path.endswith("-v3"):
            raise variants.WorktreeError("no space left on device")
        return real(project_path, path, ref)

    monkeypatch.setattr(variants, "create_scratch_worktree", flaky)

    assert await _compete(arena.story) is None
    assert _read(arena.story.path, "feature.py") == "# the failed attempt: bad\n"
    assert not os.path.exists(variant_worktree_path(arena.story.base, KEY, 1))  # the one that was made is removed
    assert any("too few scratch worktrees" in e["title"] for e in list_activity(KEY))


@pytest.mark.usefixtures("forge_db")
async def test_a_gate_that_cannot_judge_one_attempt_does_not_sink_the_others(arena, monkeypatch):
    script = arena.build({1: "good", 2: "good"})
    real_gate = script.gate

    async def gate(**kwargs):
        if kwargs["worktree_path"].endswith("-v1"):
            raise RuntimeError("toolchain crashed")
        return await real_gate(**kwargs)

    monkeypatch.setattr(variants, "run_deterministic_gate", gate)
    result = await _compete(arena.story, count=2)
    assert [v["chosen"] for v in result["variants"]] == [False, True]
    assert result["variants"][0]["error"].startswith("the gate could not judge it")


@pytest.mark.usefixtures("forge_db")
async def test_a_cancelled_run_still_removes_its_scratch_worktrees(arena, monkeypatch):
    started = asyncio.Event()

    async def hangs(**kwargs):
        started.set()
        await asyncio.sleep(60)

    arena.build({1: "good", 2: "good"})
    monkeypatch.setattr(variants, "run_dev", hangs)

    task = asyncio.create_task(_compete(arena.story, count=2))
    await asyncio.wait_for(started.wait(), 30)
    assert os.path.isdir(variant_worktree_path(arena.story.base, KEY, 1))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for i in (1, 2):
        assert not os.path.exists(variant_worktree_path(arena.story.base, KEY, i))
    assert "_variants" not in _git(arena.story.repo, "worktree", "list", "--porcelain")
    assert _read(arena.story.path, "feature.py") == "# the failed attempt: bad\n"


# --- in the graph --------------------------------------------------------------------------


@pytest.fixture()
def run(postgres_url, project_id, fake_agents, monkeypatch):
    """The real graph and checkpointer with scripted agents: the gate fails `fails`
    times, then passes."""
    set_config("dev_variants", "2")

    class Run:
        pass

    r = Run()
    r.dev_calls, r.gate_calls, r.variant_calls = 0, 0, 0
    r.gate_fails = 2
    r.variants_result = {"file_changes": [{"path": "winner.py"}], "dev_history": [{"winner": True}], "story_worktrees": {}}

    async def dev(**kwargs):
        r.dev_calls += 1
        return {"file_changes": [{"path": "a.py"}], "dev_history": [], "messages": [], "story_worktrees": {}}

    async def gate(**kwargs):
        r.gate_calls += 1
        if r.gate_calls <= r.gate_fails:
            return {"passed": False, "issues": [{"reason": "tests fail"}], "required_fixes": ["fix the tests"]}
        return {"passed": True, "issues": [], "required_fixes": []}

    async def compete(**kwargs):
        r.variant_calls += 1
        return r.variants_result

    monkeypatch.setattr(graph_module, "run_dev", dev)
    monkeypatch.setattr(graph_module, "run_deterministic_gate", gate)
    monkeypatch.setattr(graph_module, "run_dev_variants", compete)

    async def go(**state_overrides):
        state = initial_state(KEY, "Add a widget", "/repo")
        state.update({"worktree_path": "/tmp/anywhere", "project_id": project_id, "started_clean": True, **state_overrides})
        async with checkpointer_from_url(postgres_url) as checkpointer:
            return await compile_graph(checkpointer, require_plan_approval=False).ainvoke(
                state, {"configurable": {"thread_id": f"variants-{uuid.uuid4()}"}},
            )

    r.go = go
    return r


@pytest.mark.usefixtures("forge_db")
async def test_two_gate_failures_in_a_row_send_the_run_to_competing_attempts_then_back_through_the_gate(run):
    final = await run.go()

    assert final["status"] == "completed"
    assert run.variant_calls == 1
    assert run.dev_calls == 2  # the first attempt and one repair; the third round was the competition
    assert run.gate_calls == 3  # two failures, then the adopted winner
    assert final["dev_history"] == [{"winner": True}]
    assert final["variants_done"] is True and final["variants_pending"] is False


@pytest.mark.usefixtures("forge_db")
async def test_an_adopted_winner_that_fails_the_gate_gets_one_repair_round_not_a_fresh_budget(run):
    run.gate_fails = 4  # the two that bring in the competition, the winner, and its first repair

    final = await run.go()

    assert final["status"] == "failed"
    assert run.variant_calls == 1  # once per run
    assert run.dev_calls == 3  # attempt, repair, and the winner's one repair


@pytest.mark.usefixtures("forge_db")
async def test_nothing_adopted_carries_on_exactly_as_before(run):
    run.variants_result = None
    run.gate_fails = 3  # never passes

    final = await run.go()

    assert run.variant_calls == 1
    assert final["status"] == "failed"  # the third gate failure, as it always was
    assert run.dev_calls == 3 and run.gate_calls == 3  # the competition cost the run nothing


@pytest.mark.usefixtures("forge_db")
async def test_competing_attempts_that_raise_never_fail_the_run(run, monkeypatch):
    async def explode(**kwargs):
        raise RuntimeError("scratch disk full")

    monkeypatch.setattr(graph_module, "run_dev_variants", explode)
    run.gate_fails = 3

    final = await run.go()

    assert final["status"] == "failed" and run.dev_calls == 3  # as without them, not an exception
    assert any("Competing attempts failed" in e["title"] for e in list_activity(KEY))


@pytest.mark.usefixtures("forge_db")
async def test_off_by_default_the_graph_never_competes(run):
    set_config("dev_variants", "1")
    run.gate_fails = 3
    final = await run.go()
    assert run.variant_calls == 0 and final["status"] == "failed"
    assert not final.get("variants_pending")


@pytest.mark.parametrize("override", [{"started_clean": False}, {"integration_paths": ["src/a.py"]}])
@pytest.mark.usefixtures("forge_db")
async def test_a_worktree_holding_other_work_never_competes(run, override):
    run.gate_fails = 3
    final = await run.go(**override)
    assert run.variant_calls == 0 and final["status"] == "failed"


@pytest.mark.usefixtures("forge_db")
async def test_the_trigger_can_be_moved_earlier(run):
    set_config("dev_variants_after_gate_failures", "1")
    run.gate_fails = 1

    final = await run.go()

    assert final["status"] == "completed" and run.variant_calls == 1
    assert run.dev_calls == 1  # no repair round: the first failure went straight to the competition
