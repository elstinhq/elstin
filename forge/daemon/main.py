"""The single-process poll loop: JIRA poll -> reconciler -> dispatch.

No control-plane/worker split (a fleet-scale horizontal-concurrency concern,
irrelevant at one-tenant/one-machine scale) — this is the whole daemon. `forge.app.create_app`'s lifespan starts `run_forever()` as a
background asyncio task in the same process that serves the settings UI/API
(see the plan's "single process" design), so there is exactly one thing to
`docker compose up`.

Dispatch is deliberately synchronous within one poll cycle — a story's
LangGraph run is awaited to completion (or to its next interrupt) before
moving to the next intent. That's a real simplification: two independent
stories in the same wave dispatch one after another, not concurrently, and a
crash mid-cycle just means the next cycle's `ResumeStory` picks the story
back up from its last checkpoint. Real per-story concurrency is a Phase 2
concern (asyncio.gather over a wave's DispatchStory/ResumeStory intents) —
the reconciler already emits the full ready set for a wave in one call, so
parallelizing the dispatch loop later doesn't change anything upstream of it.

The poll loop is not the only caller, though: the API's /start and /approve
routes run `execute_story_action` as a background task on the same event
loop, so a second run for a story already in flight is possible while the
first is awaited. Two things keep that from happening. A story is
"dispatched" for exactly as long as its run is in flight (set when the run
is claimed, replaced by the run's outcome when it ends), and the keys of the
runs executing in this process are what `OrchestratorContext.live_thread_ids`
carries — so the reconciler answers a "dispatched" story that is really
running with NoOp, and one left "dispatched" by a crash with a resume.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from forge.config.store import get_config, resolve_bool_config
from forge.db.engine import get_database_url
from forge.orchestration.graph import checkpointer_from_url, compile_graph
from forge.orchestration.integration import integration_branch_name, merge_story_into_integration
from forge.orchestration.reconciler import (
    Blocked,
    DispatchStory,
    EpicSnapshot,
    Intent,
    MergeIntegrationBranch,
    NoOp,
    OpenPullRequest,
    OrchestratorContext,
    ParkForGate,
    ResumeStory,
    StorySnapshot,
    describe_story_action,
    epic_completion_status,
    plan_orchestration,
)
from forge.agents.roles.qa import format_qa_report_comment
from forge.orchestration.state import initial_state
from forge.services.github_service import GitHubService, parse_github_remote, push_branch
from forge.services.project_guide import project_guide_for_run
from forge.services.run_activity import append_activity
from forge.services.ticket_source import get_ticket_source, ticket_source_configured
from forge.services.usage import record_run_outcome
from forge.state.manager import StateManager
from forge.tools.codebase_skeleton import build_workspace_skeleton
from forge.tools.git_tools import (
    CODE_FORGE_CO_AUTHOR, GitMergeError, branch_exists, commit_all, get_branch_diff_summary,
)
from forge.tools.workspace import build_repo_roots
from forge.tools.worktree import create_worktree

logger = logging.getLogger("forge.daemon")

# The graph's terminal RunStatus values map onto the coarser StoryStatus the
# reconciler reasons about (see forge/orchestration/reconciler.py's module
# docstring for why these are different vocabularies). "completed" (dev ->
# verify -> qa all passed) becomes "awaiting_pr_open" — the reconciler's
# pr_open_gate holds it there until a human clears it (or require_pr_open_
# approval is off), at which point handle_intent's OpenPullRequest branch
# opens the real GitHub PR (see _maybe_open_pull_request below) when GitHub
# is configured and moves the story on to "awaiting_pr_review", where the
# epic-level PR-review gate applies before merge — either gate is correct
# whether or not a real PR exists behind it yet.
_GRAPH_STATUS_TO_STORY_STATUS = {
    "awaiting_approval": "awaiting_plan_approval",
    "awaiting_clarification": "awaiting_plan_approval",
    "completed": "awaiting_pr_open",
    # A QA story produces a report, not code — there is no branch to review
    # and no PR to open, so it goes straight to a terminal-ok status rather
    # than through the PR-review gate. Its output reaches humans as a
    # comment on the QA ticket (see _post_qa_report).
    "qa_completed": "done",
    "failed": "failed",
}

# Cap on the cumulative integration-branch diff handed to epic QA. Much
# larger than get_branch_diff_summary's default, because this diff spans
# every story in the epic rather than one branch's worth of work; the QA
# prompt truncates to the same figure.
_QA_DIFF_MAX_CHARS = 60_000

# Cap on the planner brief (story text + every sub-task's text). Generous:
# a story with a dozen sub-tasks is exactly the case this exists for, and
# truncating the breakdown would silently drop scope the team agreed on.
_MAX_BRIEF_CHARS = 40_000

# Cap on the codebase skeleton appended after the ticket text — its own
# budget, so a big repo's map can never eat into the ticket's 40k.
_MAX_SKELETON_CHARS = 12_000


def build_snapshot(sm: StateManager, epic_key: str) -> EpicSnapshot:
    epic = sm.get_epic(epic_key)
    stories = sm.list_stories_for_epic(epic_key)
    story_snapshots = {
        s["jira_key"]: StorySnapshot(
            jira_key=s["jira_key"], status=s["status"], thread_id=s["thread_id"], coverage_gap=s["coverage_gap"]
        )
        for s in stories
    }
    return EpicSnapshot(
        jira_key=epic_key,
        levels=epic["levels"],
        stories=story_snapshots,
        manual_gate=epic["manual_gate"],
    )


# Keys of the stories whose LangGraph run is executing in THIS process right
# now. thread_id == story_key by construction, so these are also the live
# thread ids the reconciler asks about. In-memory on purpose: a restart kills
# every run, and the "dispatched" rows it leaves behind are exactly what the
# reconciler resumes from their checkpoint. The lock is because the API's
# sync routes call `build_context` from a worker thread while the event loop
# claims and releases runs.
_live_runs: set[str] = set()
_live_runs_lock = threading.Lock()


def live_run_keys() -> frozenset[str]:
    with _live_runs_lock:
        return frozenset(_live_runs)


def claim_run(story_key: str) -> bool:
    """True if this caller now owns the story's run, False if one is already
    in flight. Check-and-add under one lock, with no await between them."""
    with _live_runs_lock:
        if story_key in _live_runs:
            return False
        _live_runs.add(story_key)
        return True


def release_run(story_key: str) -> None:
    with _live_runs_lock:
        _live_runs.discard(story_key)
        _run_tasks.pop(story_key, None)
        _cancel_requested.discard(story_key)


# The asyncio task executing each live run's graph, and the stories a person
# has asked to cancel. Each run gets its own task (see _run_or_resume_story)
# precisely so it can be cancelled without cancelling whatever awaits it — the
# daemon's poll loop awaits runs inline, and cancelling *that* would stop the
# daemon. Guarded by the same lock as the claims.
_run_tasks: dict[str, asyncio.Task] = {}
_cancel_requested: set[str] = set()


def _register_run_task(story_key: str, task: asyncio.Task) -> None:
    with _live_runs_lock:
        _run_tasks[story_key] = task


def _request_cancel(story_key: str) -> asyncio.Task | None:
    """Ask the story's run to stop. Returns its task, or None if there is no
    run in flight in this process. Must be called on the event loop's thread
    (asyncio's cancel is not thread-safe) — an `async def` route does."""
    with _live_runs_lock:
        task = _run_tasks.get(story_key)
        if task is None or task.done():
            return None
        _cancel_requested.add(story_key)
    task.cancel()
    return task


async def cancel_run(story_key: str, *, wait_seconds: float = 30.0) -> bool:
    """Cancel a story's run and wait for it to be wound down, so the caller
    sees the story's final state. False if there was no run to cancel.

    The run stops at its next await: a model call is abandoned at once. A tool
    already executing in a worker thread (a build, a test run) cannot be
    interrupted and is left to finish — its result is discarded."""
    if _request_cancel(story_key) is None:
        return False
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while story_key in live_run_keys() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    return True


def build_context(sm: StateManager, epic: dict) -> OrchestratorContext:
    project_id = epic["project_id"]
    return OrchestratorContext(
        dispatch_paused=False,
        live_thread_ids=live_run_keys(),
        require_plan_approval=resolve_bool_config("require_plan_approval", project_id),
        require_pr_open_approval=resolve_bool_config("require_pr_open_approval", project_id),
        require_pr_review=resolve_bool_config("require_pr_review", project_id),
        approved_story_keys=sm.approved_story_keys_for_epic(epic["jira_key"]),
    )


def _workspace_roots(sm: StateManager, epic: dict, default_repo: str | None) -> tuple[str, dict[str, str]]:
    """(default label, {label: path}) for every repo on this project.

    No repo is bound to the epic or the story. The planner reads all of
    them and the approved plan decides which ones dev writes, so this is
    simply the whole project. `default_repo` only fixes which repo an
    UNPREFIXED path means — the epic's assigned folder. Assembled here
    because the daemon owns the DB, the same separation the QA context
    follows."""
    repos = sm.list_project_repos(epic["project_id"]) if epic.get("project_id") else []
    return build_repo_roots(repos, default_repo)


def _build_story_brief(
    sm: StateManager,
    story: dict,
    repo_path: str | None = None,
    repo_roots: dict[str, str] | None = None,
    default_label: str = "",
) -> str:
    """The `user_request` handed to the planner.

    Previously this was the story's one-line summary and nothing else — the
    description JIRA had already fetched was stored and never used. It now
    carries the full ticket text plus every sub-task's, which is where the
    concrete implementation detail actually lives: a sub-task like PROJ-3486
    names the exact file to create, while its parent story only states the
    user-facing acceptance criteria. Both matter, and the sub-tasks are the
    reason forge doesn't need to dispatch them separately.

    When `repo_path` is given, a pre-computed codebase skeleton (every
    source file's path and top-level declarations —
    forge/tools/codebase_skeleton.py) is appended after the ticket text, so
    the planner starts from a map of the repo instead of spending its
    first turns discovering the directory tree. Walks the repo, so call it
    off the event loop (asyncio.to_thread) from async code.
    """
    parts = [story.get("summary") or story["jira_key"]]
    if story.get("description"):
        parts.append(story["description"].strip())

    subtasks = sm.list_subtasks(story["jira_key"])
    if subtasks:
        parts.append(
            f"## Implementation breakdown ({len(subtasks)} JIRA sub-tasks)\n"
            "These sub-tasks belong to this ticket and are delivered together with it, in "
            "this one change. They are not separate deliverables — treat them as the "
            "agreed breakdown of the work, and cover all of them."
        )
        for subtask in subtasks:
            block = f"### {subtask['jira_key']} — {subtask.get('summary') or ''}".rstrip()
            if subtask.get("description"):
                block += "\n" + subtask["description"].strip()
            parts.append(block)

    brief = "\n\n".join(parts)[:_MAX_BRIEF_CHARS]

    if repo_path:
        try:
            skeleton = build_workspace_skeleton(
                repo_path, repo_roots, default_label=default_label, max_chars=_MAX_SKELETON_CHARS
            )
        except Exception as exc:  # noqa: BLE001 — a map is a head start, never a reason not to plan
            logger.warning("story %s: could not build codebase skeleton for %s: %s", story["jira_key"], repo_path, exc)
            skeleton = ""
        if skeleton:
            brief = f"{brief}\n\n{skeleton}"
    return brief


def _build_qa_context(sm: StateManager, epic: dict, qa_story: dict) -> dict:
    """Assembles everything the epic-QA review needs. Lives here rather
    than in the QA role because the daemon is the layer that owns the DB
    and the repo — the roles stay free of StateManager imports.

    Only plain JSON-safe values go in: this ends up inside a LangGraph
    checkpoint, which is serialized to Postgres.
    """
    stories = sm.list_stories_for_epic(epic["jira_key"])
    # Top-level dev stories only, each carrying its own sub-tasks nested.
    # Flattening them into one list would misrepresent the epic to the
    # reviewer as ~30 independent deliverables when the team scoped 5.
    subtasks_by_parent: dict[str, list[dict]] = {}
    for story in stories:
        if story.get("parent_key"):
            subtasks_by_parent.setdefault(story["parent_key"], []).append(story)

    dev_stories = sorted(
        (s for s in stories if s.get("kind") != "qa" and s.get("parent_key") is None),
        key=lambda s: (s.get("wave", 0), s["jira_key"]),
    )

    branch = epic.get("integration_branch")
    # Every repo any of this epic's stories actually wrote to. An epic
    # split across an API repo and a UI repo has the same integration
    # branch name in each, so reviewing only the epic's default repo would
    # hide half the epic from the reviewer.
    story_repos: list[str] = []
    for candidate in [epic.get("repo"), *(s.get("repo") for s in stories)]:
        if candidate and candidate not in story_repos:
            story_repos.append(candidate)

    branch_diff: dict = {"base": None, "branch": None, "changed_files": [], "diff": "", "diff_truncated": False}
    other_repo_diffs: list[dict] = []
    if branch and story_repos:
        # The budget is split across repos so a two-repo epic does not
        # double the QA payload.
        per_repo = max(_QA_DIFF_MAX_CHARS // len(story_repos), 4_000)
        for index, repo_path in enumerate(story_repos):
            if not branch_exists(repo_path, branch):
                # Normal, not an error: a repo only grows the integration
                # branch once a story in it merges.
                continue
            diff = get_branch_diff_summary(repo_path, branch, max_chars=per_repo)
            if index == 0:
                branch_diff = diff
            else:
                other_repo_diffs.append({**diff, "repo": repo_path})
    if not branch:
        # No integration branch means nothing has merged yet. QA runs in the
        # last wave so this should not happen, but an epic consisting only
        # of QA tickets would hit it — report the absence rather than
        # fabricating an empty diff that reads as "no changes were made".
        logger.warning("epic %s has no integration branch — QA will review an empty diff", epic["jira_key"])

    return {
        "qa_story": {k: qa_story.get(k) for k in ("jira_key", "summary", "description")},
        "epic": {k: epic.get(k) for k in ("jira_key", "summary", "description")},
        "dev_stories": [
            {
                **{k: s.get(k) for k in ("jira_key", "summary", "description", "status", "wave")},
                "subtasks": [
                    {k: t.get(k) for k in ("jira_key", "summary", "description")}
                    for t in sorted(subtasks_by_parent.get(s["jira_key"], []), key=lambda t: t["jira_key"])
                ],
            }
            for s in dev_stories
        ],
        "branch_diff": branch_diff,
        # Empty for a single-repo epic, so the QA payload is unchanged there.
        "other_repo_diffs": other_repo_diffs,
    }


def _post_qa_report(sm: StateManager, epic: dict, story_key: str, qa_report: dict) -> None:
    """Comments the QA report onto the QA ticket. Best-effort by the same
    reasoning as PR creation: forge's own state is the source of truth for
    progress, and a JIRA outage should not fail a run whose work is done.

    This is the report's only consumer — without it the epic-QA stage would
    cost a model call and produce nothing anyone reads."""
    if not qa_report:
        return
    project_id = epic["project_id"]
    if not ticket_source_configured(project_id):
        logger.info("story %s: ticket source not configured — QA report not posted", story_key)
        return
    try:
        body = format_qa_report_comment(qa_report, epic_key=epic["jira_key"])
        if get_ticket_source(project_id).add_comment(story_key, body):
            logger.info("story %s: QA report posted to the ticket source", story_key)
    except Exception as exc:  # noqa: BLE001 — best-effort, the run itself already succeeded
        logger.warning("story %s: could not post QA report to the ticket source: %s", story_key, exc)


async def _run_or_resume_story(sm: StateManager, epic: dict, story: dict, *, resume: bool) -> bool:
    """Runs a story's graph, keeping its status truthful while it runs.

    The status is "dispatched" from the moment the run is claimed until the
    run's outcome replaces it (`_execute_story_run`). Before this, a story
    kept showing its previous status — "awaiting_plan_approval", with the
    approval still set — for the whole run, which read as stuck, let a second
    /start pick it up again, and made the ticket sync treat a story whose
    plan was being implemented as not yet started.

    The approval that unparked the run is consumed here, when it is acted on,
    not after: it must not still be standing when the run lands on its next
    gate, or a single click would clear that one too.

    Returns False, having done nothing, if a run for this story is already in
    flight in this process."""
    story_key = story["jira_key"]
    if not claim_run(story_key):
        logger.info("story %s: a run is already in flight — not starting a second", story_key)
        return False
    previous = (story["status"], story.get("parked_reason"), bool(story.get("gate_approved")))
    try:
        # thread_id == story_key by construction; recorded now, not after the
        # run, so a snapshot taken mid-run already names the live thread.
        sm.set_story_thread_id(story_key, story_key)
        sm.set_story_gate_approved(story_key, False)
        sm.set_story_status(story_key, "dispatched")
        # Its own task, so a person can cancel this run without cancelling the
        # caller that awaits it (see cancel_run).
        run = asyncio.create_task(_execute_story_run(sm, epic, story, resume=resume), name=f"story-run:{story_key}")
        _register_run_task(story_key, run)
        try:
            await run
        except asyncio.CancelledError:
            with _live_runs_lock:
                asked = story_key in _cancel_requested
            # cancelling() counts cancel() calls made on THIS task: zero means the
            # run was cancelled underneath us by a person; anything else is the
            # caller being cancelled (a shutdown), which must still unwind.
            if asked and not asyncio.current_task().cancelling():
                _mark_cancelled(sm, story_key)
                return True
            _unwind_failed_run(sm, story_key, previous)
            raise
        except BaseException:
            _unwind_failed_run(sm, story_key, previous)
            raise
    finally:
        release_run(story_key)
    return True


def _unwind_failed_run(sm: StateManager, story_key: str, previous: tuple[str, str | None, bool]) -> None:
    try:
        _restore_unstarted(sm, story_key, previous)
    except Exception:  # noqa: BLE001 — never mask the run's own error with a failed cleanup
        logger.exception("story %s: could not restore its status after a failed run", story_key)


def _mark_cancelled(sm: StateManager, story_key: str) -> None:
    """A person cancelled the run: the story is failed, with the reason, so the
    existing Retry (a full redispatch) and Remove both apply. If the run had
    already written its outcome before the cancel landed, that outcome stands."""
    try:
        current = sm.get_story(story_key)
        if current is None or current["status"] != "dispatched":
            return
        sm.set_story_status(story_key, "failed", parked_reason="cancelled", failure_stage="run")
        append_activity(story_key, type="status", phase="system", title="Run cancelled", detail="Stopped by a person. Retry starts it over from planning.")
        logger.warning("story %s: run cancelled by a person", story_key)
    except Exception:  # noqa: BLE001 — the run is stopped either way
        logger.exception("story %s: could not record the cancellation", story_key)


def _restore_unstarted(sm: StateManager, story_key: str, previous: tuple[str, str | None, bool]) -> None:
    """A run that raised before the story reached any new status goes back to
    what it was — approval included — so the next cycle retries it exactly as
    it would have before the run was claimed (a pending story is dispatched
    again, an approved one resumed). A run that already wrote its outcome is
    left alone: rolling that back would re-run finished work."""
    current = sm.get_story(story_key)
    if current is None or current["status"] != "dispatched":
        return
    status, parked_reason, approved = previous
    sm.set_story_status(story_key, status, parked_reason=parked_reason)
    sm.set_story_gate_approved(story_key, approved)


async def _execute_story_run(sm: StateManager, epic: dict, story: dict, *, resume: bool) -> None:
    story_key = story["jira_key"]
    story_kind = story.get("kind") or "dev"
    worktree_path = story.get("worktree_path")
    # The repo THIS story writes to: its own override, or the epic's
    # default (resolved in forge/state/manager_story.py). Every git
    # operation below is story-scoped, so none of them may use
    # epic["repo"] — an epic whose stories span repos would otherwise
    # branch, commit and open PRs against whichever repo the epic happened
    # to default to. It is only a DEFAULT, though: the plan may name other
    # repos by prefix, and dev creates a worktree in each of those too.
    story_repo = story.get("repo") or epic["repo"]

    # The whole project's repos. Which of them this story writes is
    # decided by its plan, inside the run (forge/tools/workspace.py).
    default_label, repo_roots = await asyncio.to_thread(_workspace_roots, sm, epic, story_repo)
    known_worktrees = {
        entry["repo_label"]: {
            "repo_path": entry["repo_path"],
            "worktree_path": entry["worktree_path"],
            "branch": entry["branch"],
        }
        for entry in await asyncio.to_thread(sm.list_story_worktrees, story_key)
    }

    if not resume:
        worktrees_base_dir = get_config("worktrees_base_dir") or "/data/repos"
        wt = await asyncio.to_thread(
            create_worktree,
            story_repo,
            story_key=story_key,
            summary=story["summary"] or story_key,
            worktrees_base_dir=worktrees_base_dir,
            # The ticket's own type/labels and its short reference, so the
            # branch reads `feat/3-show-the-currency` rather than naming
            # the internal story key (forge/tools/worktree.py).
            issue_type=story.get("issue_type"),
            labels=story.get("labels"),
            external_source=story.get("external_source"),
            external_id=story.get("external_id"),
            base_ref=epic.get("integration_branch"),
            repo_label=default_label,
        )
        worktree_path = wt["worktree_path"]
        sm.record_story_worktree(
            story_key, repo_label=default_label, repo_path=story_repo,
            worktree_path=worktree_path, branch=wt["branch"], is_default=True,
        )

    # thread_id == story_key by construction — one LangGraph run per story,
    # named for exactly the thing it implements.

    config = {"configurable": {"thread_id": story_key}}
    async with checkpointer_from_url(get_database_url()) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=resolve_bool_config("require_plan_approval", epic["project_id"]))
        if resume:
            # Re-supplied on every resume rather than trusted from the
            # checkpoint: a repo may have been added to or removed from the
            # project since this run was first dispatched, and a stale
            # absolute path would fail the path guard rather than degrade.
            patch = {
                "worktree_path": worktree_path,
                "story_kind": story_kind,
                "repo_roots": repo_roots,
                "default_repo_label": default_label,
                "story_worktrees": known_worktrees,
                # Empty on every resume except the one right after a human
                # answers (POST /api/stories/{key}/answer, forge/state/
                # manager_story.py's set_story_clarification_answers) — a
                # plain plan-approval resume has nothing here, same as
                # before this existed. Harmless to re-supply on a later,
                # unrelated resume of the same thread: only planner_node
                # reads it, and planner only ever runs once per run.
                "clarification_answers": story.get("clarification_answers") or [],
            }
            if story_kind == "qa":
                # Rebuilt rather than reused from the checkpoint: more
                # stories may have merged since this run was first
                # dispatched, and QA reviewing a stale diff would be worse
                # than the cost of recomputing it.
                patch["qa_context"] = await asyncio.to_thread(_build_qa_context, sm, epic, story)
            await graph.aupdate_state(config, patch)
            result = await graph.ainvoke(None, config)
        else:
            brief = await asyncio.to_thread(
                _build_story_brief, sm, story, story_repo, repo_roots, default_label
            )
            state = initial_state(story_key, brief, story_repo)
            state["worktree_path"] = worktree_path
            state["repo_roots"] = repo_roots
            state["default_repo_label"] = default_label
            state["project_guide"] = await project_guide_for_run(sm, epic["project_id"], repo_roots)
            state["story_worktrees"] = known_worktrees
            state["story_kind"] = story_kind
            if story_kind == "qa":
                state["qa_context"] = await asyncio.to_thread(_build_qa_context, sm, epic, story)
            result = await graph.ainvoke(state, config)

    # Record every repo the run turned out to write. dev creates a worktree
    # per repo the approved plan names, so this set is only known after the
    # run — that is the whole point of the plan deciding rather than
    # configuration.
    for label, entry in (result.get("story_worktrees") or {}).items():
        if not entry.get("worktree_path") or not entry.get("branch"):
            continue
        await asyncio.to_thread(
            sm.record_story_worktree,
            story_key,
            repo_label=label,
            repo_path=entry.get("repo_path") or "",
            worktree_path=entry["worktree_path"],
            branch=entry["branch"],
            is_default=(label == default_label),
        )

    # The graph's own terminal status, not the coarser story status below:
    # "completed" vs "qa_completed" vs "awaiting_approval" are different
    # run shapes, and the mapping would flatten them.
    record_run_outcome(story_key, outcome=result.get("status"))
    new_status = _GRAPH_STATUS_TO_STORY_STATUS.get(result.get("status"))
    if new_status is None:
        # A status the graph should never end on. Mapping it to "dispatched"
        # (the old default) would have the reconciler resume it every cycle
        # forever, re-spending tokens each time; failing is loud and a human
        # can retry it.
        logger.error(
            "story %s: run ended on unexpected status %r — marking it failed", story_key, result.get("status")
        )
        new_status = "failed"
    # Nothing to commit, review or merge: dev declared the planned files already
    # do what the ticket needs, the gate accepted that and the reviewer did not
    # object, and the worktree has no changes. That is a finished story, not one
    # waiting to open an empty pull request (which GitHub would refuse).
    finished_reason: str | None = None
    declared = result.get("declared_unchanged") or {}
    if new_status == "awaiting_pr_open" and declared and not result.get("file_changes"):
        new_status, finished_reason = "done", "already_satisfied"
        append_activity(
            story_key, type="status", phase="system", title="Finished with no changes: the ticket is already satisfied",
            detail="\n".join(f"{path}: {reason}" for path, reason in sorted(declared.items()))[:4000],
        )
    # "run" — no PR exists yet at this point (a run only reaches
    # "awaiting_pr_open" on success), so retry_story's recovery for this
    # stage is a full redispatch. See forge/state/manager_story.py.
    sm.set_story_status(
        story_key, new_status, parked_reason=finished_reason, failure_stage="run" if new_status == "failed" else None,
    )
    # The graph's own status, not new_status: "awaiting_clarification" and a
    # plain "awaiting_approval" both collapse to "awaiting_plan_approval"
    # above, but only the former has a question worth showing. Set on every
    # completion (None otherwise) so a story that plans cleanly on a later
    # run does not keep showing a question from an earlier one.
    sm.set_story_clarification_questions(
        story_key, result.get("clarification_questions") if result.get("status") == "awaiting_clarification" else None,
    )
    if new_status == "failed":
        logger.error("story %s failed: %s", story_key, result.get("error"))
    else:
        logger.info("story %s -> %s", story_key, new_status)

    if new_status == "awaiting_pr_open":
        coverage_gap, coverage_gap_detail = _extract_coverage_gap(result)
        if coverage_gap:
            logger.info("story %s: coverage gap only — %s", story_key, coverage_gap_detail)
        sm.set_story_coverage_gap(story_key, coverage_gap, detail=coverage_gap_detail)

        # The dev/verifier loop only ever checks the worktree's working-tree
        # diff (git_tools.list_changed_files/get_all_diffs both see
        # uncommitted changes fine) — nothing commits it. Without this, a
        # story can pass verification for real and still push a branch
        # identical to its base, since `git push` transfers commits, not
        # working-tree state: GitHub then rejects PR creation with "No
        # commits between main and <branch>" despite the worktree having
        # real, verified changes sitting there uncommitted.
        # One commit per repo the story wrote. A story spanning two repos
        # has two branches, and each needs its own commit before its own
        # push and PR — git has no cross-repo commit.
        message = f"{story_key}: {story['summary'] or story_key}"
        author = _commit_author()
        for entry in await asyncio.to_thread(sm.list_story_worktrees, story_key) or [
            {"repo_label": default_label, "worktree_path": worktree_path}
        ]:
            try:
                commit_sha = await asyncio.to_thread(commit_all, entry["worktree_path"], message, author=author)
                if commit_sha:
                    logger.info("story %s: committed %s in repo %s", story_key, commit_sha, entry["repo_label"])
            except GitMergeError as exc:
                sm.set_story_status(story_key, "failed", failure_stage="run")
                logger.error(
                    "story %s: failed to commit changes in repo %s: %s", story_key, entry["repo_label"], exc
                )
                return

    # Posted whether the run finished clean or failed on the merged branch's
    # test suite — a failing epic is exactly when the report is most worth
    # reading, so withholding it on failure would be backwards.
    if story_kind == "qa":
        await asyncio.to_thread(_post_qa_report, sm, epic, story_key, result.get("qa_report") or {})


def _commit_author() -> tuple[str, str] | None:
    """The configured (name, email) commits are credited to, or None when
    either is unset — commit_all then authors as code_forge itself."""
    name = (get_config("git_author_name") or "").strip()
    email = (get_config("git_author_email") or "").strip()
    return (name, email) if name and email else None


def _extract_coverage_gap(result: dict) -> tuple[bool, str | None]:
    """A completed run's verifier_report nests the deterministic gate under
    `deterministic_gate` once the LLM-review stage ran (the normal case for
    a coverage-only miss, since that no longer counts as a required_fix —
    see language_hooks._default_verify) but not when the gate itself
    short-circuited the run before LLM review, where verifier_report IS the
    gate. Falling back to verifier_report itself covers both shapes."""
    verifier_report = result.get("verifier_report") or {}
    gate = verifier_report.get("deterministic_gate") or verifier_report
    return bool(gate.get("coverage_gap")), gate.get("coverage_gap_detail")


def _maybe_open_pull_request(
    sm: StateManager, epic: dict, story_key: str, branch: str | None,
    *, story_repo: str, repo_label: str = "", coverage_gap: bool = False, coverage_gap_detail: str | None = None,
) -> None:
    """Push the story branch and open a real GitHub PR once the pr_open_gate
    has cleared (see handle_intent's OpenPullRequest branch), when GitHub is
    configured — best-effort: forge's own approval gates (POST
    /api/stories/{key}/approve) still govern progress regardless of whether
    a real PR exists behind it, so a push/API failure here doesn't block the
    run. It's recorded on Story.pr_open_error, though (cleared on the next
    successful attempt), so it's visible in the UI rather than only in this
    process's logs."""
    if not branch or not GitHubService.is_configured():
        return
    remote = parse_github_remote(story_repo)
    if not remote:
        msg = "origin is not a GitHub remote — PR not opened"
        logger.info("story %s: %s", story_key, msg)
        sm.set_story_pr_open_error(story_key, msg)
        return
    owner, repo_name = remote

    pushed, push_error = push_branch(story_repo, branch)
    if not pushed:
        sm.set_story_pr_open_error(story_key, f"git push failed: {push_error}")
        return

    body = f"Automated implementation for {story_key}."
    if coverage_gap:
        body += (
            "\n\n⚠️ **Coverage gap**: tests pass, but coverage fell short of the "
            "repo's threshold" + (f" ({coverage_gap_detail})" if coverage_gap_detail else "") + "."
        )

    if _commit_author():
        body += f"\n\n{CODE_FORGE_CO_AUTHOR}"

    try:
        gh = GitHubService()
        pr = gh.create_pull_request(
            owner, repo_name,
            head=branch,
            base=epic.get("integration_branch") or "main",
            title=f"{story_key}: {branch}",
            body=body,
        )
        sm.upsert_pull_request(story_key, repo_label=repo_label, number=pr["number"], url=pr["url"], state="open")
        sm.set_story_pr_open_error(story_key, None)
        logger.info("story %s: opened PR %s", story_key, pr["url"])
    except Exception as exc:  # noqa: BLE001 — best-effort, story still progresses via forge's own gate
        logger.warning("story %s: PR creation failed: %s", story_key, exc)
        sm.set_story_pr_open_error(story_key, f"PR creation failed: {exc}")


async def handle_intent(sm: StateManager, epic: dict, intent) -> None:
    epic_key = epic["jira_key"]

    # The intents were planned from a snapshot; a ticket removed since (see
    # forge/daemon/ticket_removal.py) no longer has the story they name.
    story_key = getattr(intent, "story_key", None)
    if story_key and not isinstance(intent, NoOp) and sm.get_story(story_key) is None:
        logger.info("story %s no longer exists — skipping %s", story_key, type(intent).__name__)
        return

    if isinstance(intent, DispatchStory):
        await _run_or_resume_story(sm, epic, sm.get_story(intent.story_key), resume=False)

    elif isinstance(intent, ResumeStory):
        # The approval that unparked this resume (if any) is consumed inside
        # the run's claim — otherwise one approval click would silently clear
        # every later gate for this story too (e.g. plan-approval also
        # pre-clearing the PR-review gate it hasn't reached yet).
        await _run_or_resume_story(sm, epic, sm.get_story(intent.story_key), resume=True)

    elif isinstance(intent, ParkForGate):
        story = sm.get_story(intent.story_key)
        sm.set_story_status(intent.story_key, story["status"], parked_reason=intent.reason)
        logger.info("story %s parked: %s", intent.story_key, intent.reason)

    elif isinstance(intent, OpenPullRequest):
        story = sm.get_story(intent.story_key)
        # One PR per repo the story wrote. Two repos means two PRs, both of
        # which have to be green before the merge step proceeds.
        for entry in _story_repo_targets(sm, epic, story):
            await asyncio.to_thread(
                _maybe_open_pull_request,
                sm, epic, intent.story_key, entry["branch"],
                story_repo=entry["repo_path"], repo_label=entry["repo_label"],
                coverage_gap=story["coverage_gap"], coverage_gap_detail=story["coverage_gap_detail"],
            )
        sm.set_story_status(intent.story_key, "awaiting_pr_review")
        # Same reasoning as ResumeStory: the approval that cleared the
        # pr_open_gate must not silently pre-clear the pr_review_gate too.
        sm.set_story_gate_approved(intent.story_key, False)

    elif isinstance(intent, MergeIntegrationBranch):
        story = sm.get_story(intent.story_key)
        if await asyncio.to_thread(_ci_checks_block_merge, sm, epic, intent.story_key):
            return
        worktrees_base_dir = get_config("worktrees_base_dir") or "/data/repos"
        # Merged inside each repo the story wrote. The integration branch
        # NAME is per epic, so an epic spanning two repos gets a same-named
        # branch in each — which is what makes a cross-repo epic reviewable
        # as one unit.
        targets = _story_repo_targets(sm, epic, story)
        results = [
            await asyncio.to_thread(
                merge_story_into_integration, entry["repo_path"], epic_key, entry["branch"], worktrees_base_dir,
                branch=epic.get("integration_branch") or None,
            )
            for entry in targets
        ]
        failed = [(entry, res) for entry, res in zip(targets, results) if not res["applied"]]
        result = {
            "applied": bool(results) and not failed,
            "error": "; ".join(f"{entry['repo_label']}: {res['error']}" for entry, res in failed) or None,
        }
        if result["applied"]:
            sm.set_story_status(intent.story_key, "merged")
            sm.set_story_gate_approved(intent.story_key, False)
            for entry in targets:
                await asyncio.to_thread(
                    _maybe_merge_pull_request, sm, epic, intent.story_key, entry["repo_path"], entry["repo_label"]
                )
            if not epic.get("integration_branch"):
                sm.set_integration_branch(epic_key, integration_branch_name(
                    epic_key, external_source=epic.get("external_source"), external_id=epic.get("external_id"),
                ))
            logger.info("story %s merged into %s", intent.story_key, integration_branch_name(epic_key))
        else:
            # "merge" — the run itself already succeeded (a PR may already
            # be open), so retry_story's recovery for this stage only
            # re-attempts the merge rather than redispatching the story.
            sm.set_story_status(intent.story_key, "failed", failure_stage="merge")
            logger.error("story %s merge failed: %s", intent.story_key, result["error"])

    elif isinstance(intent, NoOp):
        logger.debug("noop epic=%s reason=%s story=%s", epic_key, intent.reason, intent.story_key)


def check_story_action(sm: StateManager, epic: dict, story_key: str) -> Intent | Blocked:
    """Manual-run-mode eligibility check — the same dependency-ordering and
    epic-halt rules poll_once's automatic scan applies, but scoped to one
    requested story instead of a background pass over every epic. Cheap and
    side-effect-free, so API routes (forge/api/run_routes.py) can call it
    inline before deciding whether to spawn the actual (possibly
    minutes-long) execution via execute_story_action below."""
    snapshot = build_snapshot(sm, epic["jira_key"])
    ctx = build_context(sm, epic)
    return describe_story_action(snapshot, ctx, story_key)


async def execute_story_action(sm: StateManager, epic: dict, intent: Intent) -> None:
    """Runs an already-validated intent (from check_story_action) and keeps
    Epic.phase accurate afterward. This is the manual-run-mode counterpart
    to poll_once's per-cycle epic-phase bookkeeping: an epic whose project
    has auto_run_enabled off is never visited by the automatic scan, so
    nothing else would notice it just became complete or failed. Intended
    to run as a FastAPI BackgroundTask — may take minutes (a real LangGraph
    run), so callers must not await it inline in the request path."""
    epic_key = epic["jira_key"]
    await handle_intent(sm, epic, intent)
    if sm.get_epic(epic_key) is None:  # removed while the action ran
        return
    completion = epic_completion_status(build_snapshot(sm, epic_key))
    if completion == "complete":
        sm.set_epic_phase(epic_key, "complete")
        logger.info("epic %s complete", epic_key)
    elif completion == "failed":
        sm.set_epic_phase(epic_key, "failed")
        logger.error("epic %s failed", epic_key)


def _ci_checks_block_merge(sm: StateManager, epic: dict, story_key: str) -> bool:
    """CI-aware merge gate — don't merge on your own say-so, check real CI
    evidence. Returns True when the merge should NOT proceed this cycle.

    A story that wrote several repos has a PR in each, and ALL of them must
    be green: merging the UI half of a change while the API half is red is
    exactly the split-brain that per-repo PRs make possible, so the gate is
    the conjunction rather than any one PR.

    - No PR opened yet, or GitHub not configured: nothing to check — never blocks.
    - Any PR pending: block, but leave gate_approved untouched so the next
      poll cycle re-checks automatically — no re-approval needed once CI
      finishes.
    - Any PR failed: block AND clear gate_approved, forcing a human to look
      at it and re-approve — a silently-retried merge attempt against a
      known failure would just spin forever doing nothing useful.
    """
    prs = [pr for pr in sm.list_pull_requests(story_key) if pr.get("number")]
    if not prs or not GitHubService.is_configured():
        return False

    story = sm.get_story(story_key)
    by_label = {entry["repo_label"]: entry for entry in _story_repo_targets(sm, epic, story or {})}

    pending: list[str] = []
    failed: list[str] = []
    for pr in prs:
        target = by_label.get(pr["repo_label"])
        repo_path = target["repo_path"] if target else ((story or {}).get("repo") or epic["repo"])
        remote = parse_github_remote(repo_path)
        if not remote:
            continue
        owner, repo_name = remote
        try:
            status = GitHubService().get_pr_check_status(owner, repo_name, pr["number"])
        except Exception as exc:  # noqa: BLE001 — an unreadable CI status is "not ready", never "merge blind"
            logger.warning("story %s: could not read CI status for PR #%s: %s", story_key, pr["number"], exc)
            pending.append(f"#{pr['number']}")
            continue
        if status == "pending":
            pending.append(f"#{pr['number']}")
        elif status != "success":
            failed.append(f"#{pr['number']}")

    if failed:
        sm.set_story_gate_approved(story_key, False)
        sm.set_story_status(story_key, "awaiting_pr_review", parked_reason="ci_failed")
        logger.warning(
            "story %s: CI failed for PR(s) %s — approval cleared, needs human attention",
            story_key, ", ".join(failed),
        )
        return True
    if pending:
        sm.set_story_status(story_key, "awaiting_pr_review", parked_reason="ci_pending")
        logger.info("story %s: waiting on CI for PR(s) %s", story_key, ", ".join(pending))
        return True
    return False


def _story_repo_targets(sm: StateManager, epic: dict, story: dict) -> list[dict[str, str]]:
    """Every (repo, branch) this story produced, for the commit/PR/merge
    steps to walk.

    Falls back to the story's single repo and branch for a story
    dispatched before per-repo worktrees existed, so an in-flight story
    survives the upgrade."""
    entries = sm.list_story_worktrees(story["jira_key"])
    if entries:
        return [
            {"repo_label": e["repo_label"], "repo_path": e["repo_path"], "branch": e["branch"]}
            for e in entries
            if e.get("branch")
        ]
    if story.get("branch"):
        return [
            {
                "repo_label": "",
                "repo_path": story.get("repo") or epic["repo"],
                "branch": story["branch"],
            }
        ]
    return []


def _maybe_merge_pull_request(
    sm: StateManager, epic: dict, story_key: str, story_repo: str, repo_label: str = ""
) -> None:
    """Merge the real GitHub PR (if one was opened) once the local
    integration-branch merge has already succeeded. Best-effort, same
    reasoning as _maybe_open_pull_request: forge's own state is the source
    of truth for story progress, a real PR's state trails it."""
    existing = sm.get_pull_request(story_key, repo_label)
    if not existing or not existing.get("number") or not GitHubService.is_configured():
        sm.upsert_pull_request(story_key, repo_label=repo_label, state="merged")
        return

    remote = parse_github_remote(story_repo)
    if not remote:
        sm.upsert_pull_request(story_key, repo_label=repo_label, state="merged")
        return
    owner, repo_name = remote

    try:
        gh = GitHubService()
        result = gh.merge_pull_request(owner, repo_name, existing["number"])
        sm.upsert_pull_request(story_key, repo_label=repo_label, number=existing["number"], url=existing.get("url"), state="merged" if result.get("merged") else "open")
        if not result.get("merged"):
            logger.warning("story %s: GitHub reported PR #%s as not merged", story_key, existing["number"])
    except Exception as exc:  # noqa: BLE001 — best-effort; local merge already succeeded
        logger.warning("story %s: PR merge via GitHub API failed: %s", story_key, exc)
        sm.upsert_pull_request(story_key, repo_label=repo_label, number=existing["number"], url=existing.get("url"), state="merged")


async def poll_once(sm: StateManager | None = None) -> None:
    sm = sm or StateManager()
    # No OpenAI-key gate here: dispatch itself (creating a worktree,
    # advancing a story's status) never touches forge/llm/registry.py —
    # only the planner/dev/verifier/qa role functions do, once dispatched,
    # and a missing key surfaces there (as DevLoopError/
    # PlannerLoopError/a RuntimeError from forge/services/generation.py)
    # rather than blocking dispatch globally. JIRA configuredness is
    # project-scoped and checked per-epic below via
    # JiraService(project_id=...).
    for epic_key in sm.list_active_epic_keys():
        epic = sm.get_epic(epic_key)
        if not epic["repo"]:
            # No repo is bound to an epic or a story (forge/tools/workspace.py)
            # — this is only the default for unprefixed paths, and
            # forge/services/epic_sync.py already assigns it automatically
            # from most syncs. This is the safety net for the epic that
            # slips through that — synced before its project had any repo,
            # or before this backfill existed — so dispatch is never stuck
            # on a human picking a folder that no longer has a UI control.
            repos = sm.list_project_repos(epic["project_id"]) if epic.get("project_id") else []
            if repos:
                sm.set_epic_project_repo(epic_key, repos[0]["id"])
                epic = sm.get_epic(epic_key)
            else:
                logger.info("epic %s: project has no repo cloned yet — skipping", epic_key)
                continue
        if not resolve_bool_config("auto_run_enabled", epic["project_id"]):
            # This project runs in manual mode — nothing here autopicks
            # ready work. Progress happens only via the /start route and
            # the approve/retry follow-through in forge/api/run_routes.py
            # (see check_story_action/execute_story_action below).
            continue
        snapshot = build_snapshot(sm, epic_key)
        ctx = build_context(sm, epic)

        for intent in plan_orchestration(snapshot, ctx):
            if isinstance(intent, NoOp) and intent.reason == "epic_complete":
                sm.set_epic_phase(epic_key, "complete")
                logger.info("epic %s complete", epic_key)
            elif isinstance(intent, NoOp) and intent.reason == "epic_failed":
                sm.set_epic_phase(epic_key, "failed")
                logger.error("epic %s failed at story %s", epic_key, intent.story_key)
            else:
                await handle_intent(sm, epic, intent)


async def run_forever(*, interval_seconds: int = 15) -> None:
    logger.info("forge daemon starting, poll interval=%ss", interval_seconds)
    while True:
        try:
            await poll_once()
        except Exception:
            logger.exception("poll cycle failed")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
