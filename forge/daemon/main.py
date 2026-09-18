"""The single-process poll loop: JIRA poll -> reconciler -> dispatch.

No control-plane/worker split (a fleet-scale horizontal-concurrency concern,
irrelevant at one-tenant/one-machine scale) — this is the whole daemon. `forge.app.create_app`'s lifespan starts `run_forever()` as a
background asyncio task in the same process that serves the settings UI/API
(see the plan's "single process" design), so there is exactly one thing to
`docker compose up`.

Dispatch is deliberately synchronous within one poll cycle — a story's
LangGraph run is awaited to completion (or to its next interrupt) before
moving to the next intent. That's a real simplification: two independent
stories in the same wave dispatch one after another, not concurrently, so
`OrchestratorContext.live_thread_ids` is always empty at the start of every
cycle (nothing is ever "still running" between cycles) and a crash mid-cycle
just means the next cycle's `ResumeStory` picks the story back up from its
last checkpoint. Real per-story concurrency is a Phase 2 concern
(asyncio.gather over a wave's DispatchStory/ResumeStory intents) — the
reconciler already emits the full ready set for a wave in one call, so
parallelizing the dispatch loop later doesn't change anything upstream of it.
"""
from __future__ import annotations

import asyncio
import logging

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
from forge.services.jira_service import JiraService
from forge.services.usage import record_run_outcome
from forge.state.manager import StateManager
from forge.tools.codebase_skeleton import build_workspace_skeleton
from forge.tools.git_tools import GitMergeError, branch_exists, commit_all, get_branch_diff_summary
from forge.tools.workspace import build_reference_roots
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


def build_context(sm: StateManager, epic: dict) -> OrchestratorContext:
    project_id = epic["project_id"]
    return OrchestratorContext(
        dispatch_paused=False,
        live_thread_ids=frozenset(),
        require_plan_approval=resolve_bool_config("require_plan_approval", project_id),
        require_pr_open_approval=resolve_bool_config("require_pr_open_approval", project_id),
        require_pr_review=resolve_bool_config("require_pr_review", project_id),
        approved_story_keys=sm.approved_story_keys_for_epic(epic["jira_key"]),
    )


def _reference_roots(sm: StateManager, epic: dict, story_repo: str | None) -> dict[str, str]:
    """{label: path} for every repo on this project except the one THIS
    story writes to.

    Keyed on the story's repo rather than the epic's, because they differ
    exactly when multi-repo support matters: an epic carrying an API story
    and a UI story has each of them writing to its own repo and reading
    the other. Assembled here because the daemon owns the DB, the same
    separation the QA context follows."""
    if not epic.get("project_id"):
        return {}
    repos = sm.list_project_repos(epic["project_id"])
    return build_reference_roots(repos, story_repo)


def _build_story_brief(
    sm: StateManager, story: dict, repo_path: str | None = None, reference_roots: dict[str, str] | None = None
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
            skeleton = build_workspace_skeleton(repo_path, reference_roots, max_chars=_MAX_SKELETON_CHARS)
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
    if not JiraService.is_configured(project_id):
        logger.info("story %s: JIRA not configured — QA report not posted", story_key)
        return
    try:
        body = format_qa_report_comment(qa_report, epic_key=epic["jira_key"])
        if JiraService(project_id=project_id).add_comment(story_key, body):
            logger.info("story %s: QA report posted to JIRA", story_key)
    except Exception as exc:  # noqa: BLE001 — best-effort, the run itself already succeeded
        logger.warning("story %s: could not post QA report to JIRA: %s", story_key, exc)


async def _run_or_resume_story(sm: StateManager, epic: dict, story: dict, *, resume: bool) -> None:
    story_key = story["jira_key"]
    story_kind = story.get("kind") or "dev"
    worktree_path = story.get("worktree_path")
    # The repo THIS story writes to: its own override, or the epic's
    # default (resolved in forge/state/manager_story.py). Every git
    # operation below is story-scoped, so none of them may use
    # epic["repo"] — an epic whose stories span repos would otherwise
    # branch, commit and open PRs against whichever repo the epic happened
    # to default to.
    story_repo = story.get("repo") or epic["repo"]

    if not resume:
        worktrees_base_dir = get_config("worktrees_base_dir") or "/data/repos"
        wt = await asyncio.to_thread(
            create_worktree,
            story_repo,
            story_key=story_key,
            summary=story["summary"] or story_key,
            worktrees_base_dir=worktrees_base_dir,
            base_ref=epic.get("integration_branch"),
        )
        worktree_path = wt["worktree_path"]
        sm.set_story_worktree(story_key, worktree_path=worktree_path, branch=wt["branch"])

    # thread_id == story_key by construction — one LangGraph run per story,
    # named for exactly the thing it implements.
    # Read-only sibling repos for this project, so a UI story can read the
    # API repo it talks to (forge/tools/workspace.py). Empty for a
    # single-repo project, which leaves every path below unchanged.
    reference_roots = await asyncio.to_thread(_reference_roots, sm, epic, story_repo)

    config = {"configurable": {"thread_id": story_key}}
    async with checkpointer_from_url(get_database_url()) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=resolve_bool_config("require_plan_approval", epic["project_id"]))
        if resume:
            # Re-supplied on every resume rather than trusted from the
            # checkpoint: a repo may have been added to or removed from the
            # project since this run was first dispatched, and a stale
            # absolute path would fail the path guard rather than degrade.
            patch = {"worktree_path": worktree_path, "story_kind": story_kind, "reference_roots": reference_roots}
            if story_kind == "qa":
                # Rebuilt rather than reused from the checkpoint: more
                # stories may have merged since this run was first
                # dispatched, and QA reviewing a stale diff would be worse
                # than the cost of recomputing it.
                patch["qa_context"] = await asyncio.to_thread(_build_qa_context, sm, epic, story)
            await graph.aupdate_state(config, patch)
            result = await graph.ainvoke(None, config)
        else:
            brief = await asyncio.to_thread(_build_story_brief, sm, story, story_repo, reference_roots)
            state = initial_state(story_key, brief, story_repo)
            state["worktree_path"] = worktree_path
            state["reference_roots"] = reference_roots
            state["story_kind"] = story_kind
            if story_kind == "qa":
                state["qa_context"] = await asyncio.to_thread(_build_qa_context, sm, epic, story)
            result = await graph.ainvoke(state, config)

    sm.set_story_thread_id(story_key, story_key)
    # The graph's own terminal status, not the coarser story status below:
    # "completed" vs "qa_completed" vs "awaiting_approval" are different
    # run shapes, and the mapping would flatten them.
    record_run_outcome(story_key, outcome=result.get("status"))
    new_status = _GRAPH_STATUS_TO_STORY_STATUS.get(result.get("status"), "dispatched")
    # "run" — no PR exists yet at this point (a run only reaches
    # "awaiting_pr_open" on success), so retry_story's recovery for this
    # stage is a full redispatch. See forge/state/manager_story.py.
    sm.set_story_status(story_key, new_status, failure_stage="run" if new_status == "failed" else None)
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
        try:
            commit_sha = await asyncio.to_thread(
                commit_all, worktree_path, f"{story_key}: {story['summary'] or story_key}"
            )
            logger.info("story %s: committed worktree changes %s", story_key, commit_sha)
        except GitMergeError as exc:
            sm.set_story_status(story_key, "failed", failure_stage="run")
            logger.error("story %s: failed to commit worktree changes: %s", story_key, exc)
            return

    # Posted whether the run finished clean or failed on the merged branch's
    # test suite — a failing epic is exactly when the report is most worth
    # reading, so withholding it on failure would be backwards.
    if story_kind == "qa":
        await asyncio.to_thread(_post_qa_report, sm, epic, story_key, result.get("qa_report") or {})


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
    *, story_repo: str, coverage_gap: bool = False, coverage_gap_detail: str | None = None,
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

    try:
        gh = GitHubService()
        pr = gh.create_pull_request(
            owner, repo_name,
            head=branch,
            base=epic.get("integration_branch") or "main",
            title=f"{story_key}: {branch}",
            body=body,
        )
        sm.upsert_pull_request(story_key, number=pr["number"], url=pr["url"], state="open")
        sm.set_story_pr_open_error(story_key, None)
        logger.info("story %s: opened PR %s", story_key, pr["url"])
    except Exception as exc:  # noqa: BLE001 — best-effort, story still progresses via forge's own gate
        logger.warning("story %s: PR creation failed: %s", story_key, exc)
        sm.set_story_pr_open_error(story_key, f"PR creation failed: {exc}")


async def handle_intent(sm: StateManager, epic: dict, intent) -> None:
    epic_key = epic["jira_key"]

    if isinstance(intent, DispatchStory):
        await _run_or_resume_story(sm, epic, sm.get_story(intent.story_key), resume=False)

    elif isinstance(intent, ResumeStory):
        await _run_or_resume_story(sm, epic, sm.get_story(intent.story_key), resume=True)
        # The approval that unparked this resume (if any) is consumed —
        # otherwise one approval click would silently clear every later
        # gate for this story too (e.g. plan-approval also pre-clearing
        # the PR-review gate it hasn't reached yet).
        sm.set_story_gate_approved(intent.story_key, False)

    elif isinstance(intent, ParkForGate):
        story = sm.get_story(intent.story_key)
        sm.set_story_status(intent.story_key, story["status"], parked_reason=intent.reason)
        logger.info("story %s parked: %s", intent.story_key, intent.reason)

    elif isinstance(intent, OpenPullRequest):
        story = sm.get_story(intent.story_key)
        await asyncio.to_thread(
            _maybe_open_pull_request,
            sm, epic, intent.story_key, story["branch"],
            story_repo=story.get("repo") or epic["repo"],
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
        result = await asyncio.to_thread(
            # Merged inside the story's own repo. The integration branch
            # NAME is per epic, so an epic spanning two repos gets a
            # same-named branch in each — which is what makes a cross-repo
            # epic reviewable as one unit.
            merge_story_into_integration, story.get("repo") or epic["repo"], epic_key, story["branch"], worktrees_base_dir
        )
        if result["applied"]:
            sm.set_story_status(intent.story_key, "merged")
            sm.set_story_gate_approved(intent.story_key, False)
            await asyncio.to_thread(_maybe_merge_pull_request, sm, epic, intent.story_key)
            if not epic.get("integration_branch"):
                sm.set_integration_branch(epic_key, integration_branch_name(epic_key))
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

    - No PR opened yet, or GitHub not configured: nothing to check — never blocks.
    - CI pending: block, but leave gate_approved untouched so the next poll
      cycle re-checks automatically — no re-approval needed once CI finishes.
    - CI failed: block AND clear gate_approved, forcing a human to look at
      it and re-approve — a silently-retried merge attempt against a known
      failure would just spin forever doing nothing useful.
    """
    pr = sm.get_pull_request(story_key)
    if not pr or not pr.get("number") or not GitHubService.is_configured():
        return False
    story = sm.get_story(story_key)
    remote = parse_github_remote((story or {}).get("repo") or epic["repo"])
    if not remote:
        return False
    owner, repo_name = remote

    try:
        status = GitHubService().get_pr_check_status(owner, repo_name, pr["number"])
    except Exception as exc:  # noqa: BLE001 — treat an unreadable CI status as "not ready" rather than merging blind
        logger.warning("story %s: could not read CI status for PR #%s: %s", story_key, pr["number"], exc)
        return True

    if status == "success":
        return False
    if status == "pending":
        sm.set_story_status(story_key, "awaiting_pr_review", parked_reason="ci_pending")
        logger.info("story %s: waiting on CI for PR #%s", story_key, pr["number"])
        return True

    sm.set_story_gate_approved(story_key, False)
    sm.set_story_status(story_key, "awaiting_pr_review", parked_reason="ci_failed")
    logger.warning("story %s: CI failed for PR #%s — approval cleared, needs human attention", story_key, pr["number"])
    return True


def _maybe_merge_pull_request(sm: StateManager, epic: dict, story_key: str) -> None:
    """Merge the real GitHub PR (if one was opened) once the local
    integration-branch merge has already succeeded. Best-effort, same
    reasoning as _maybe_open_pull_request: forge's own state is the source
    of truth for story progress, a real PR's state trails it."""
    existing = sm.get_pull_request(story_key)
    if not existing or not existing.get("number") or not GitHubService.is_configured():
        sm.upsert_pull_request(story_key, state="merged")
        return

    story = sm.get_story(story_key)
    remote = parse_github_remote((story or {}).get("repo") or epic["repo"])
    if not remote:
        sm.upsert_pull_request(story_key, state="merged")
        return
    owner, repo_name = remote

    try:
        gh = GitHubService()
        result = gh.merge_pull_request(owner, repo_name, existing["number"])
        sm.upsert_pull_request(story_key, number=existing["number"], url=existing.get("url"), state="merged" if result.get("merged") else "open")
        if not result.get("merged"):
            logger.warning("story %s: GitHub reported PR #%s as not merged", story_key, existing["number"])
    except Exception as exc:  # noqa: BLE001 — best-effort; local merge already succeeded
        logger.warning("story %s: PR merge via GitHub API failed: %s", story_key, exc)
        sm.upsert_pull_request(story_key, number=existing["number"], url=existing.get("url"), state="merged")


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
            logger.info("epic %s has no folder assigned yet — skipping (set one via PUT /api/epics/%s/repo)", epic_key, epic_key)
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
