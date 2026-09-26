"""The single-process poll loop: JIRA poll -> reconciler -> dispatch.

No control-plane/worker split (a fleet-scale horizontal-concurrency concern,
irrelevant at one-tenant/one-machine scale) — this is the whole daemon. `forge.app.create_app`'s lifespan starts `run_forever()` as a
background asyncio task in the same process that serves the settings UI/API
(see the plan's "single process" design), so there is exactly one thing to
`docker compose up`.

Stories run in parallel. The reconciler returns every story whose
dependencies have merged; `poll_once` starts each run (and each merge onto
the epic branch) as its own background task, up to `max_parallel_stories`
at once across all epics, and returns without waiting for them. A story that
becomes ready when another merges starts on the next poll. A crash just
means the next cycle's `ResumeStory` picks a story back up from its last
checkpoint, and a merge left half-done is simply merged again.

Landing a finished story is the merge queue's job (`_merge_story_into_epic`,
one story at a time per epic): bring it up to date with the epic branch,
re-verify, fast-forward — locally. The only pull request is the epic's,
opened once every story has merged (`_finish_epic`).

The poll loop is not the only caller: the API's /start and /approve routes
run `execute_story_action` as a background task on the same event loop. Two
things keep a story from running twice. A story is "dispatched" for exactly
as long as its run is in flight (set when the run is claimed, replaced by
the run's outcome when it ends), and the keys of the runs and merges
executing in this process are what `OrchestratorContext.live_thread_ids`
carries — so the reconciler answers one that is really in flight with NoOp,
and one left "dispatched" by a crash with a resume.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

from forge.config.store import get_config, get_int_config, resolve_bool_config
from forge.db.engine import get_database_url
from forge.orchestration.graph import checkpointer_from_url, compile_graph
from forge.orchestration.integration import (
    ensure_integration_worktree, epic_branch_changes_since, fast_forward_epic, integration_branch_name,
    update_story_from_epic,
)
from forge.orchestration.reconciler import (
    Blocked,
    DispatchStory,
    EpicSnapshot,
    Intent,
    MergeIntoEpic,
    NoOp,
    OrchestratorContext,
    ParkForGate,
    ResumeStory,
    StorySnapshot,
    describe_story_action,
    epic_completion_status,
    plan_orchestration,
)
from forge.agents.roles.qa import format_qa_report_comment
from forge.agents.roles.verifier import format_gate_feedback, run_deterministic_gate
from forge.orchestration.state import initial_state
from forge.services.epic_sync import stored_dependencies
from forge.services.github_service import GitHubService, parse_github_remote, push_branch
from forge.services.jira_service import issue_key_sort_key
from forge.services.project_guide import project_guide_for_run
from forge.services.run_activity import append_activity
from forge.services.ticket_source import get_ticket_source, ticket_source_configured
from forge.services.usage import record_run_outcome
from forge.state.manager import StateManager
from forge.tools.codebase_skeleton import build_workspace_skeleton
from forge.tools.git_tools import (
    CODE_FORGE_CO_AUTHOR, GitMergeError, branch_exists, commit_all, get_branch_diff_summary,
)
from forge.tools.workspace import REFERENCE_SIGIL, build_repo_roots
from forge.tools.worktree import create_worktree, default_branch, resolve_base_ref, run_git

logger = logging.getLogger("forge.daemon")

# The graph's terminal RunStatus values map onto the coarser StoryStatus the
# reconciler reasons about (see forge/orchestration/reconciler.py's module
# docstring for why these are different vocabularies). "completed" (dev ->
# gate -> review all passed) becomes "ready_to_merge": the story's work is
# committed on its branch, and the merge queue (_merge_story_into_epic)
# brings it up to date with the epic branch, re-verifies it and lands it —
# locally. No story opens a pull request; the epic does, once (see
# forge/orchestration/integration.py).
_GRAPH_STATUS_TO_STORY_STATUS = {
    "awaiting_approval": "awaiting_plan_approval",
    "awaiting_clarification": "awaiting_plan_approval",
    "completed": "ready_to_merge",
    # A QA story produces a report, not code — there is no branch to merge,
    # so it goes straight to a terminal-ok status. Its output reaches humans
    # as a comment on the QA ticket (see _post_qa_report) and in the epic's
    # pull request.
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

# Cap on the epic's own description in a story's brief (_epic_context_for_brief).
_MAX_EPIC_CHARS = 12_000


def build_snapshot(sm: StateManager, epic_key: str) -> EpicSnapshot:
    epic = sm.get_epic(epic_key)
    stories = sm.list_stories_for_epic(epic_key)
    dependencies = stored_dependencies(
        epic["levels"] or [], {s["jira_key"]: s for s in stories if s.get("parent_key") is None}
    )
    story_snapshots = {
        s["jira_key"]: StorySnapshot(
            jira_key=s["jira_key"], status=s["status"], thread_id=s["thread_id"],
            depends_on=tuple(dependencies.get(s["jira_key"], [])),
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
    # After the story's own text, and inside the same cap, so the story
    # always keeps priority over its epic.
    epic_context = _epic_context_for_brief(sm, story)
    if epic_context and len(brief) < _MAX_BRIEF_CHARS:
        brief = f"{brief}\n\n{epic_context}"[:_MAX_BRIEF_CHARS]

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


def _epic_context_for_brief(sm: StateManager, story: dict) -> str:
    """The epic this story belongs to, for the planner: what the whole change
    is for, and what the other stories in it do.

    A story's ticket usually states one slice ("add the currency column"),
    while the epic carries the why and the shared vocabulary ("multi-item
    pricing on the results list"). Without it a planner guessed the terms
    the rest of the epic uses, or planned work a sibling story owns. The
    sibling list says which stories are already merged — their code is in
    this story's worktree, which branches from the epic branch — so the
    plan builds on them instead of redoing them.

    Empty for a lone story that is its own epic (same key: there is no
    separate epic text and no sibling)."""
    epic_key = story.get("epic_key")
    if not epic_key or epic_key == story["jira_key"]:
        return ""
    epic = sm.get_epic(epic_key)
    if epic is None:
        return ""
    siblings = sorted(
        (
            s for s in sm.list_stories_for_epic(epic_key)
            if s.get("parent_key") is None and s["jira_key"] != story["jira_key"]
        ),
        key=lambda s: issue_key_sort_key(s["jira_key"]),
    )
    if not (epic.get("summary") or epic.get("description") or siblings):
        return ""  # nothing the story's own ticket does not already say

    parts = [
        f"## Epic {epic_key}: {epic.get('summary') or ''}".rstrip(),
        "This story is one part of the epic below. Use the epic for context — what the whole change is "
        "for, and the names and constraints it sets — but implement only this story's own scope.",
    ]
    if epic.get("description"):
        parts.append(epic["description"].strip()[:_MAX_EPIC_CHARS])

    if siblings:
        depends_on = set(story.get("depends_on") or [])
        lines = [
            "### Other stories in this epic",
            "Each is delivered by its own run. Do not implement their scope. The ones marked merged are "
            "already in the epic branch, so their code is in your worktree — build on it.",
        ]
        for sibling in siblings:
            state = (
                "merged into the epic branch" if sibling["status"] == "merged"
                else "finished" if sibling["status"] in ("done", "skipped")
                else "not merged yet"
            )
            relation = "; this story depends on it" if sibling["jira_key"] in depends_on else ""
            lines.append(f"- {sibling['jira_key']}: {sibling.get('summary') or ''} [{state}{relation}]")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


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
        # Every story branches from the epic branch, created here on the
        # epic's first dispatch: stories that start in parallel then share one
        # base, and a story that waited for another starts with that one's
        # code already in its worktree.
        epic_branch = epic.get("integration_branch") or integration_branch_name(
            epic["jira_key"], external_source=epic.get("external_source"), external_id=epic.get("external_id"),
        )
        await asyncio.to_thread(
            ensure_integration_worktree, story_repo, epic["jira_key"], worktrees_base_dir, branch=epic_branch,
        )
        if not epic.get("integration_branch"):
            sm.set_integration_branch(epic["jira_key"], epic_branch)
            epic = {**epic, "integration_branch": epic_branch}
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
    # waiting to merge an empty branch.
    finished_reason: str | None = None
    declared = result.get("declared_unchanged") or {}
    if new_status == "ready_to_merge" and declared and not result.get("file_changes"):
        new_status, finished_reason = "done", "already_satisfied"
        append_activity(
            story_key, type="status", phase="system", title="Finished with no changes: the ticket is already satisfied",
            detail="\n".join(f"{path}: {reason}" for path, reason in sorted(declared.items()))[:4000],
        )
    # "run" — nothing has been merged at this point (a run only reaches
    # "ready_to_merge" on success), so retry_story's recovery for this stage
    # is a full redispatch. See forge/state/manager_story.py.
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

    if new_status == "ready_to_merge":
        coverage_gap, coverage_gap_detail = _extract_coverage_gap(result)
        if coverage_gap:
            logger.info("story %s: coverage gap only — %s", story_key, coverage_gap_detail)
        sm.set_story_coverage_gap(story_key, coverage_gap, detail=coverage_gap_detail)

        # The dev/verifier loop only ever checks the worktree's working-tree
        # diff (git_tools.list_changed_files/get_all_diffs both see
        # uncommitted changes fine) — nothing commits it, and the merge
        # queue moves commits, not working-tree state. One commit per repo
        # the story wrote (git has no cross-repo commit). When dev was sent
        # back to resolve a conflict with the epic branch, the merge is still
        # in progress in the worktree and this commit completes it.
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

    elif isinstance(intent, MergeIntoEpic):
        await _merge_story_into_epic(sm, epic, sm.get_story(intent.story_key))

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
        await _finish_epic(sm, sm.get_epic(epic_key) or epic)
    elif completion == "failed":
        sm.set_epic_phase(epic_key, "failed")
        logger.error("epic %s failed", epic_key)


def _story_repo_targets(sm: StateManager, epic: dict, story: dict) -> list[dict[str, str]]:
    """Every (repo, branch) this story produced, for the commit/PR/merge
    steps to walk.

    Falls back to the story's single repo and branch for a story
    dispatched before per-repo worktrees existed, so an in-flight story
    survives the upgrade."""
    entries = sm.list_story_worktrees(story["jira_key"])
    if entries:
        return [
            {
                "repo_label": e["repo_label"], "repo_path": e["repo_path"], "branch": e["branch"],
                "worktree_path": e.get("worktree_path") or "",
            }
            for e in entries
            if e.get("branch")
        ]
    if story.get("branch"):
        return [
            {
                "repo_label": "",
                "repo_path": story.get("repo") or epic["repo"],
                "branch": story["branch"],
                "worktree_path": story.get("worktree_path") or "",
            }
        ]
    return []


# --- the epic's pull request: the only thing forge sends to GitHub ----------


async def _finish_epic(sm: StateManager, epic: dict) -> None:
    """Every story has merged into the epic branch: propose it. Sets the
    epic's phase to "pr_open" once its pull request is open, or "complete"
    when there is nothing to propose or nowhere to propose it (GitHub not
    configured) — the epic branch is then ready locally. A push or API
    failure leaves the phase alone, so the next poll tries again."""
    phase = await asyncio.to_thread(_open_epic_pull_requests, sm, epic)
    if phase is not None:
        sm.set_epic_phase(epic["jira_key"], phase)
        logger.info("epic %s -> %s", epic["jira_key"], phase)


def _epic_repos(sm: StateManager, epic: dict) -> list[tuple[str, str]]:
    """(label, repo path) for every repo the epic branch may exist in: the
    epic's default repo and each repo a story wrote."""
    repos: dict[str, str] = {}
    for story in sm.list_stories_for_epic(epic["jira_key"]):
        for entry in sm.list_story_worktrees(story["jira_key"]):
            if entry.get("repo_path"):
                repos.setdefault(os.path.realpath(entry["repo_path"]), entry.get("repo_label") or "")
    if epic.get("repo"):
        repos.setdefault(os.path.realpath(epic["repo"]), "")
    return sorted(((label, path) for path, label in repos.items()), key=lambda item: item[0])


def _commits_ahead(repo_path: str, branch: str) -> int:
    base = resolve_base_ref(repo_path, None)
    count = run_git(repo_path, "rev-list", "--count", f"{base}..{branch}", check=False)
    return int(count.stdout.strip() or 0) if count.returncode == 0 else 0


def _open_epic_pull_requests(sm: StateManager, epic: dict) -> str | None:
    epic_key = epic["jira_key"]
    branch = epic.get("integration_branch")
    worth = [(label, path) for label, path in _epic_repos(sm, epic) if branch and _commits_ahead(path, branch)]
    if not worth:
        append_activity(epic_key, type="status", phase="system", title="Epic complete: no changes to propose")
        return "complete"
    if not GitHubService.is_configured():
        append_activity(
            epic_key, type="status", phase="system", title=f"Epic complete: {branch} is ready locally",
            detail="GitHub is not configured, so no pull request was opened.",
        )
        return "complete"

    existing = {row["repo_label"]: row for row in sm.list_epic_pull_requests(epic_key)}
    body = _epic_pr_body(sm, epic)
    title = f"{epic_key}: {epic.get('summary') or branch}"
    retry_later = False
    for label, repo_path in worth:
        if (existing.get(label) or {}).get("number"):
            continue
        remote = parse_github_remote(repo_path)
        if not remote:
            sm.upsert_epic_pull_request(
                epic_key, label, repo_path=repo_path, branch=branch, error="origin is not a GitHub remote",
            )
            continue
        pushed, push_error = push_branch(repo_path, branch)
        if not pushed:
            sm.upsert_epic_pull_request(epic_key, label, repo_path=repo_path, branch=branch, error=f"git push failed: {push_error}")
            retry_later = True
            continue
        try:
            pr = GitHubService().create_pull_request(
                remote[0], remote[1], head=branch, base=default_branch(repo_path), title=title, body=body,
            )
        except Exception as exc:  # noqa: BLE001 — recorded on the row and retried next poll
            sm.upsert_epic_pull_request(epic_key, label, repo_path=repo_path, branch=branch, error=f"PR creation failed: {exc}")
            retry_later = True
            continue
        sm.upsert_epic_pull_request(
            epic_key, label, repo_path=repo_path, branch=branch, number=pr["number"], url=pr["url"],
            state="open", ci_status=None, error=None,
        )
        append_activity(epic_key, type="status", phase="system", title=f"Opened the epic pull request {pr['url']}")
        logger.info("epic %s: opened %s", epic_key, pr["url"])

    if retry_later:
        return None
    opened = [row for row in sm.list_epic_pull_requests(epic_key) if row["number"]]
    return "pr_open" if opened else "complete"


def _refresh_epic_pull_requests(sm: StateManager, epic: dict) -> None:
    """CI status and state of the epic's open pull requests; the epic becomes
    "merged" once a human has merged all of them, "complete" if they were
    closed instead."""
    epic_key = epic["jira_key"]
    if not GitHubService.is_configured():
        return
    for row in sm.list_epic_pull_requests(epic_key):
        if not row["number"] or row["state"] != "open":
            continue
        remote = parse_github_remote(row["repo_path"])
        if not remote:
            continue
        try:
            gh = GitHubService()
            state = gh.get_pull_request(remote[0], remote[1], row["number"])["state"]
            ci_status = gh.get_pr_check_status(remote[0], remote[1], row["number"]) if state == "open" else row["ci_status"]
        except Exception as exc:  # noqa: BLE001 — an unreadable PR is simply looked at again next poll
            logger.warning("epic %s: could not refresh PR #%s: %s", epic_key, row["number"], exc)
            continue
        sm.upsert_epic_pull_request(epic_key, row["repo_label"], state=state, ci_status=ci_status, error=None)

    opened = [row for row in sm.list_epic_pull_requests(epic_key) if row["number"]]
    if opened and all(row["state"] == "merged" for row in opened):
        sm.set_epic_phase(epic_key, "merged")
        append_activity(epic_key, type="status", phase="system", title="Epic pull request merged")
    elif opened and not any(row["state"] == "open" for row in opened):
        sm.set_epic_phase(epic_key, "complete")
        append_activity(epic_key, type="status", phase="system", title="Epic pull request closed without merging")


def _epic_pr_body(sm: StateManager, epic: dict) -> str:
    """What the reviewer of the epic needs: what it implements, each story and
    anything notable about it, and how the code was checked."""
    stories = sorted(
        (s for s in sm.list_stories_for_epic(epic["jira_key"]) if s.get("parent_key") is None),
        key=lambda s: issue_key_sort_key(s["jira_key"]),
    )
    lines = [f"Implements {epic['jira_key']}: {epic.get('summary') or ''}".rstrip()]
    if epic.get("description"):
        lines += ["", epic["description"].strip()[:2000]]
    lines += ["", "## Stories"]
    for story in stories:
        line = f"- **{story['jira_key']}** {story.get('summary') or ''}".rstrip()
        if story.get("kind") == "qa":
            line += " — QA review (report posted on the ticket)"
        elif story.get("parked_reason") == "already_satisfied":
            line += " — already satisfied by the existing code; no change"
        elif story["status"] == "skipped":
            line += " — skipped"
        lines.append(line)
        if story.get("coverage_gap"):
            detail = f" ({story['coverage_gap_detail']})" if story.get("coverage_gap_detail") else ""
            lines.append(f"  - ⚠️ tests pass, but coverage is below the repo's threshold{detail}")
        for answer in story.get("clarification_answers") or []:
            lines.append(f"  - decided: {answer.get('question')} → {answer.get('option_label')}")
    lines += [
        "",
        "## How this was built",
        "Each story was planned, implemented and reviewed by code_forge, passed its build and planned tests, and "
        "was checked again on top of the epic branch before landing on it. The stories were merged locally; this "
        "is the only pull request for the epic.",
    ]
    if _commit_author():
        lines += ["", CODE_FORGE_CO_AUTHOR]
    return "\n".join(lines)


# --- the merge queue: landing a finished story on the epic branch ----------

# One story lands at a time per epic, so each is updated and verified against
# exactly the epic head it will fast-forward. In-process, like the run
# registry: the daemon is one process. Keyed by event loop too: an asyncio
# lock belongs to the loop it was first used on.
_epic_merge_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _epic_merge_lock(epic_key: str) -> asyncio.Lock:
    return _epic_merge_locks.setdefault((id(asyncio.get_running_loop()), epic_key), asyncio.Lock())

# How many times a story is sent back to dev over landing it (a conflict with
# the epic branch, or checks failing on the combined code) before it fails
# with failure_stage="merge" for a human to retry.
_MAX_MERGE_ATTEMPTS = 3


async def _merge_story_into_epic(sm: StateManager, epic: dict, story: dict) -> None:
    """Land a finished ("ready_to_merge") story on the epic branch, locally —
    see forge/orchestration/integration.py for the three steps and why the
    last is a fast-forward. The story's run claim is held throughout, so the
    same story is never merged twice at once, nor merged while it runs."""
    story_key = story["jira_key"]
    if not claim_run(story_key):
        return
    try:
        async with _epic_merge_lock(epic["jira_key"]):
            await _land_story(sm, sm.get_epic(epic["jira_key"]) or epic, sm.get_story(story_key) or story)
    finally:
        release_run(story_key)


async def _land_story(sm: StateManager, epic: dict, story: dict) -> None:
    story_key, epic_key = story["jira_key"], epic["jira_key"]
    sm.set_story_status(story_key, "merging")
    worktrees_base_dir = get_config("worktrees_base_dir") or "/data/repos"
    epic_branch = epic.get("integration_branch") or integration_branch_name(
        epic_key, external_source=epic.get("external_source"), external_id=epic.get("external_id"),
    )
    if not epic.get("integration_branch"):
        sm.set_integration_branch(epic_key, epic_branch)

    targets = [t for t in await asyncio.to_thread(_story_repo_targets, sm, epic, story) if t["worktree_path"]]
    if not targets:
        _fail_merge(sm, story_key, "the story has no branch to merge")
        return
    state = await _story_run_values(story_key)
    default_label = state.get("default_repo_label") or ""

    # 1. Up to date with the epic branch (stories that landed meanwhile).
    updated = False
    conflicts: list[str] = []
    changed_on_epic: list[str] = []
    for target in targets:
        # Before the update: afterwards the story contains the epic head, and
        # "what the epic changed since the story branched" reads as nothing.
        paths = await asyncio.to_thread(epic_branch_changes_since, target["worktree_path"], epic_branch)
        changed_on_epic.extend(_display_path(target["repo_label"], default_label, p) for p in paths)
        result = await asyncio.to_thread(
            update_story_from_epic, target["repo_path"], epic_key, target["worktree_path"], worktrees_base_dir,
            branch=epic_branch,
        )
        if result["error"]:
            _fail_merge(sm, story_key, f"{target['repo_label'] or 'repo'}: {result['error']}")
            return
        updated = updated or result["updated"]
        conflicts.extend(_display_path(target["repo_label"], default_label, path) for path in result["conflicts"])
    if conflicts:
        await _send_back_to_dev(
            sm, epic, story, targets, _conflict_brief(sm, epic, story, conflicts), integration_paths=conflicts,
        )
        return

    # 2. Verify the combined code — only when something new came in: with
    #    nothing new, the gate the story already passed saw exactly this.
    if updated:
        gate = await run_deterministic_gate(
            plan=state.get("plan") or {},
            worktree_path=state.get("worktree_path") or targets[0]["worktree_path"],
            file_changes=state.get("file_changes") or [],
            run_id=story_key,
            prior_build_runner=state.get("dev_build_runner") or None,
            story_worktrees=state.get("story_worktrees") or None,
            default_label=default_label,
            declared_unchanged=state.get("declared_unchanged") or None,
        )
        if not gate.get("passed"):
            attempt = int(story.get("merge_attempts") or 0) + 1
            brief = _combined_check_brief(sm, epic, story, changed_on_epic) + "\n\n" + format_gate_feedback(
                gate, attempt=attempt, max_attempts=_MAX_MERGE_ATTEMPTS,
            )
            await _send_back_to_dev(sm, epic, story, targets, brief, integration_paths=changed_on_epic)
            return

    # 3. Fast-forward the epic branch to the verified story branch.
    for target in targets:
        result = await asyncio.to_thread(
            fast_forward_epic, target["repo_path"], epic_key, target["branch"], worktrees_base_dir, branch=epic_branch,
        )
        if not result["applied"]:
            _fail_merge(sm, story_key, f"{target['repo_label'] or 'repo'}: {result['error']}")
            return
    sm.set_story_status(story_key, "merged")
    append_activity(
        story_key, type="status", phase="system", title=f"Merged into the epic branch {epic_branch}",
        detail="Brought up to date with the epic branch and re-verified first." if updated else None,
    )
    logger.info("story %s merged into %s", story_key, epic_branch)


async def _story_run_values(story_key: str) -> dict:
    """The story's LangGraph state (plan, file changes, worktrees, ...)."""
    config = {"configurable": {"thread_id": story_key}}
    async with checkpointer_from_url(get_database_url()) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        snapshot = await graph.aget_state(config)
    return dict(snapshot.values or {})


async def _send_back_to_dev(
    sm: StateManager, epic: dict, story: dict, targets: list[dict], brief: str, *, integration_paths: list[str],
) -> None:
    """Re-open the story's finished graph at dev with `brief` as its fix
    message — as if the gate had just failed, so the run continues through
    dev -> gate -> review and ends "ready_to_merge" again. `integration_paths`
    widen dev's write scope for that run: files a conflict or the combined
    check involve, which the story's own plan may not name."""
    story_key = story["jira_key"]
    attempts = sm.increment_story_merge_attempts(story_key)
    if attempts > _MAX_MERGE_ATTEMPTS:
        # Leave the worktrees clean for a retry, which starts the landing over.
        for target in targets:
            await asyncio.to_thread(_abort_merge_in_progress, target["worktree_path"])
        _fail_merge(sm, story_key, f"could not land on the epic branch after {_MAX_MERGE_ATTEMPTS} attempt(s)")
        return
    config = {"configurable": {"thread_id": story_key}}
    async with checkpointer_from_url(get_database_url()) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await graph.aupdate_state(
            config,
            {"status": "developing", "gate_feedback": brief, "gate_attempts": 0, "integration_paths": integration_paths},
            as_node="gate",
        )
    sm.set_story_status(story_key, "dispatched")
    append_activity(
        story_key, type="status", phase="system",
        title=f"Sent back to dev to land on the epic branch (attempt {attempts}/{_MAX_MERGE_ATTEMPTS})",
        detail=brief[:1000],
    )
    logger.info("story %s: sent back to dev to land (attempt %d)", story_key, attempts)


def _abort_merge_in_progress(worktree_path: str) -> None:
    if run_git(worktree_path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        run_git(worktree_path, "merge", "--abort", check=False)


def _fail_merge(sm: StateManager, story_key: str, reason: str) -> None:
    """"merge" is the failure_stage whose Retry re-attempts only the landing
    (forge/state/manager_story.py's retry_story), keeping the story's work."""
    sm.set_story_status(story_key, "failed", failure_stage="merge")
    append_activity(story_key, type="status", phase="system", title="Could not merge into the epic branch", detail=reason)
    logger.error("story %s: merge failed: %s", story_key, reason)


def _display_path(label: str, default_label: str, path: str) -> str:
    """A repo-relative path as dev's tools address it (forge/tools/workspace.py):
    bare in the story's default repo, `@label/path` in any other."""
    return path if not label or label == default_label else f"{REFERENCE_SIGIL}{label}/{path}"


def _landed_meanwhile(sm: StateManager, epic: dict, story: dict) -> str:
    merged = [
        s for s in sm.list_stories_for_epic(epic["jira_key"])
        if s["status"] == "merged" and s["jira_key"] != story["jira_key"] and s.get("parent_key") is None
    ]
    return ", ".join(f"{s['jira_key']} ({s.get('summary') or ''})" for s in merged) or "other stories"


def _conflict_brief(sm: StateManager, epic: dict, story: dict, conflicts: list[str]) -> str:
    return (
        f"MERGE CONFLICT WITH THE EPIC BRANCH (attempt {int(story.get('merge_attempts') or 0) + 1}/{_MAX_MERGE_ATTEMPTS}).\n"
        f"Your story is finished, but while you worked these stories merged into the epic branch: "
        f"{_landed_meanwhile(sm, epic, story)}. Bringing your branch up to date with it conflicted in:\n"
        + "\n".join(f"- {path}" for path in conflicts)
        + "\n\nThe merge is in progress in your worktree: each file above contains conflict markers. The side "
        "between <<<<<<< and ======= is yours; the side after ======= and before >>>>>>> is the epic branch's "
        "(the other stories' work).\n"
        "Resolve every conflict so the result does what your story AND theirs require: keep both changes where "
        "both are needed, never revert the other stories' work, and remove every marker line. You may edit these "
        "files even where your plan does not name them. Then run analyze_changed_files and finish."
    )


def _combined_check_brief(sm: StateManager, epic: dict, story: dict, changed_on_epic: list[str]) -> str:
    listed = "\n".join(f"- {path}" for path in changed_on_epic[:40]) or "- (none recorded)"
    return (
        "THE COMBINED CODE FAILS ITS CHECKS. Your story merged cleanly with the epic branch, which now also "
        f"has: {_landed_meanwhile(sm, epic, story)} — but together the code fails the checks below. Fix it so "
        "your change and theirs both work; do not revert their work. Besides your planned files you may edit "
        "the files the epic branch changed:\n" + listed
    )


async def poll_once(sm: StateManager | None = None, *, wait: bool = False) -> None:
    """One pass over every active epic. Runs and merges the reconciler asks
    for are started as background tasks (_launch), up to
    max_parallel_stories at once, and this returns without waiting for
    them: a story that becomes ready while others are still running — its
    dependency just merged — starts on the next poll, not after the slowest
    run of this one. `wait=True` awaits what this pass started (tests)."""
    sm = sm or StateManager()
    launched: list[asyncio.Task] = []
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
        if epic["phase"] == "pr_open":
            # Every story has merged and the epic's pull request is open: all
            # that is left is to notice CI results and a human's merge. Done
            # in manual-run projects too — this picks up no new work.
            await asyncio.to_thread(_refresh_epic_pull_requests, sm, epic)
            continue
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
                await _finish_epic(sm, epic)
            elif isinstance(intent, NoOp) and intent.reason == "epic_failed":
                sm.set_epic_phase(epic_key, "failed")
                logger.error("epic %s failed at story %s", epic_key, intent.story_key)
            elif isinstance(intent, (DispatchStory, ResumeStory, MergeIntoEpic)):
                task = _launch(sm, epic, intent)
                if task is not None:
                    launched.append(task)
            else:
                await handle_intent(sm, epic, intent)

    if wait and launched:
        await asyncio.gather(*launched, return_exceptions=True)


# The runs and merges poll_once started and has not seen finish, by story key.
# In-process, like the run registry: the daemon is one process.
_background: dict[str, asyncio.Task] = {}


def _launch(sm: StateManager, epic: dict, intent: Intent) -> asyncio.Task | None:
    """Start `intent` as a background task, unless that story already has one
    or max_parallel_stories are already busy (then it waits for a later
    poll). Busy counts both what this loop started and runs started from
    the API (/start, /approve), which claim the same registry."""
    key = intent.story_key
    if key in _background or key in live_run_keys():
        return None
    busy = len(set(_background) | live_run_keys())
    if busy >= max(1, get_int_config("max_parallel_stories", 3)):
        logger.info("story %s: ready, waiting for a free slot (%d busy)", key, busy)
        return None

    async def run() -> None:
        try:
            await handle_intent(sm, epic, intent)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one story's failure must not go unseen, nor stop the others
            logger.exception("story %s: %s failed", key, type(intent).__name__)
        finally:
            _background.pop(key, None)

    task = asyncio.create_task(run(), name=f"{type(intent).__name__}:{key}")
    _background[key] = task
    return task


async def cancel_background_work() -> None:
    """Cancel every run and merge poll_once started, and wait for them to
    unwind (each restores its story's status — see _run_or_resume_story)."""
    tasks = list(_background.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)




async def run_forever(*, interval_seconds: int = 15) -> None:
    logger.info("forge daemon starting, poll interval=%ss", interval_seconds)
    try:
        while True:
            try:
                await poll_once()
            except Exception:
                logger.exception("poll cycle failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await cancel_background_work()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
