"""LangGraph state schema for the planner/dev/verifier/qa workflow."""
import operator
from typing import Annotated, Any, Literal, TypedDict

RunStatus = Literal[
    "planning",
    "awaiting_clarification",
    "awaiting_approval",
    "developing",
    "verifying",
    "qa",
    "completed",
    # Terminal status for a QA story specifically. Distinct from "completed"
    # because the two mean different things downstream: "completed" means
    # code is ready to open a PR, whereas a QA story produces a report and
    # no PR at all (see elstin/daemon/main.py's status mapping).
    "qa_completed",
    "failed",
    "rejected",
]

WorkspaceMode = Literal["worktree", "in_place"]

# Which path through the graph this run takes — set by the daemon from
# Story.kind. "dev" enters at the planner; "qa" enters at the qa node and
# skips planning and implementation entirely.
StoryKind = Literal["dev", "qa"]


class FeatureRunState(TypedDict, total=False):
    run_id: str
    root_run_id: str
    parent_run_id: str | None
    attempt: int
    status: RunStatus
    user_request: str
    project_path: str
    ticket_id: int | None
    worktree_path: str | None
    # The workspace (elstin/tools/workspace.py): every repo on the project
    # at its checkout, and the label an UNPREFIXED path belongs to. No repo
    # is bound to the epic or the story — the approved plan names which
    # repos it writes, and dev creates a worktree for each. Assembled by
    # the daemon, which owns the DB, and carried as plain dicts because
    # this state is JSON in a LangGraph checkpoint.
    repo_roots: dict[str, str]
    default_repo_label: str
    # Where each repo's code is READ while planning and by dev's reads of a
    # repo it does not write: the story's own worktree where it has one,
    # else the epic branch's worktree in that repo. Never the clone's own
    # checkout, which is frozen at clone time and knows nothing of the epic
    # branch, the stories merged into it, or prior work the story continues.
    # `repo_roots` stays the clones: git operations and repo locks key on it.
    read_roots: dict[str, str]
    # {label: ref} a repo the plan names but the story has no worktree in yet
    # branches from — the epic branch in that repo, not its default branch.
    story_base_refs: dict[str, str]
    # Labels in `repo_roots` no plan may write: read-only repos and
    # reference folders (elstin/db/models/project.py's ProjectRepo). Read like
    # any repo; the plan gate refuses them as targets and dev never gets a
    # worktree in one.
    read_only_labels: list[str]
    # What already existed for this ticket before its branch was made —
    # elstin/daemon/prior_work_lookup.py's planner_prior_work, for the
    # planner prompt's prior-work block. Empty when nothing was found.
    prior_work: dict[str, Any]
    # The AGENTS.md-style guide the planner (and, through context_bundle, dev)
    # reads first: {"found", "path", "content", "generated"}. Resolved by the
    # daemon, which owns the DB; see elstin/services/project_guide.py.
    project_guide: dict[str, Any]
    # {label: {repo_path, worktree_path, branch}} — the worktrees this
    # story has so far. Grows as the plan turns out to touch more repos.
    story_worktrees: dict[str, dict[str, str]]
    workspace_mode: WorkspaceMode
    iteration: int
    max_iterations: int
    context_bundle: dict[str, Any]
    plan: dict[str, Any]
    acceptance_criteria: list[str]
    file_changes: list[dict[str, Any]]
    diffs: list[dict[str, Any]]
    verifier_report: dict[str, Any]
    qa_report: dict[str, Any]
    story_kind: StoryKind
    # Everything the epic-QA review needs that isn't derivable from this
    # run's own state: the QA ticket, the epic, the epic's implementation
    # stories and their acceptance criteria, and the cumulative integration
    # branch diff. Assembled by the daemon (which owns the DB) and passed
    # in, so elstin/agents/roles/qa.py stays free of StateManager imports —
    # same separation the other roles keep.
    qa_context: dict[str, Any]
    messages: Annotated[list[dict[str, Any]], operator.add]
    # The dev loop's own elstin/llm/types.py message history, JSON-serialized
    # (elstin/llm/types.py:history_to_json/history_from_json) — durably
    # checkpointed by LangGraph's checkpointer alongside every other
    # field here, which is what lets a turn-capped run resume from exactly
    # where it stopped (see elstin/agents/roles/dev_loop.py's module
    # docstring) instead of restarting cold. Replaces the old Claude Agent
    # SDK's dev_sdk_session_id, which asked the provider's own CLI process
    # to reload ITS server-side session — there is no equivalent
    # server-side session with a stateless Messages-API-shaped provider,
    # so elstin owns this instead.
    dev_history: list[dict[str, Any]]
    dev_build_runner: dict[str, Any]
    # Tier 1 deterministic gate (elstin/orchestration/graph.py's gate_node):
    # the compare_to_plan report from the most recent gate run, the count
    # of consecutive gate failures (reset to 0 on a pass; the run fails
    # once it reaches max_gate_attempts), and — when the gate just failed
    # — the targeted fix message dev_node hands run_dev as this
    # iteration's brief instead of a verifier report. Cleared by dev_node
    # once consumed.
    # The dev agent's live progress checklist (elstin/tools/progress.py),
    # seeded from the plan on the first pass and carried across gate/
    # verifier fix rounds so a second pass resumes the same bookkeeping
    # instead of starting from "nothing done".
    dev_progress: list[dict[str, str]]
    # {planned path: reason} — files dev declared already satisfy the ticket.
    declared_unchanged: dict[str, str]
    tier1_gate: dict[str, Any]
    gate_attempts: int
    max_gate_attempts: int
    gate_feedback: str | None
    # Paths dev may write beyond the plan's, set when the merge queue sends a
    # finished story back to land it on the epic branch (elstin/daemon/main.py's
    # _send_back_to_dev): the files a conflict or the combined-code check
    # involve, which the story's own plan may not name.
    integration_paths: list[str]
    error: str | None
    # Set on a run that failed because the provider refused calls on a limit
    # (elstin/llm/errors.py's ProviderLimit.as_state): {"kind": "rate"|"quota",
    # "reset_at": epoch seconds or None, "message"}. The daemon parks such a
    # story until the limit lifts instead of leaving it failed. Cleared when the
    # run is re-armed.
    provider_limit: dict[str, Any] | None
    # Competing dev attempts (elstin/agents/roles/dev_variants.py). `started_clean`:
    # when the run began, the story's worktrees held no uncommitted work from an
    # earlier run, so a winner may replace what is there (set by the daemon on a
    # fresh dispatch; absent on a run checkpointed before it existed, which then
    # never competes). `variants_pending`: the gate asked for them; the graph
    # routes to the variants node. `variants_done`: tried once already this run.
    started_clean: bool
    variants_pending: bool
    variants_done: bool
    approved: bool
    rejected: bool
    merge_report: dict[str, Any]
    truncated: bool
    clarification_questions: list[dict[str, Any]]
    clarification_answers: list[dict[str, Any]]
    attachment_paths: list[str]
    linked_issues_context: str | None
    acceptance_criteria_hint: list[str]
    sdk_model: str | None
    sdk_effort: str | None
    # The project the story belongs to, for the roles to resolve what a
    # project can choose for itself — which model each role runs on, and which
    # provider, endpoint and key the calls go to — instead of reading the
    # global setting. Only the id: a key must never be put in this state, which
    # is checkpointed and sent to LangSmith. None (a run
    # checkpointed before this existed) means the global settings.
    project_id: int | None


def initial_state(run_id: str, user_request: str, project_path: str) -> FeatureRunState:
    return FeatureRunState(
        run_id=run_id,
        root_run_id=run_id,
        parent_run_id=None,
        attempt=1,
        status="planning",
        user_request=user_request,
        project_path=project_path,
        worktree_path=None,
        repo_roots={},
        read_roots={},
        story_base_refs={},
        read_only_labels=[],
        prior_work={},
        default_repo_label="",
        project_guide={},
        story_worktrees={},
        workspace_mode="worktree",
        iteration=0,
        max_iterations=3,
        context_bundle={},
        plan={},
        acceptance_criteria=[],
        file_changes=[],
        diffs=[],
        verifier_report={},
        qa_report={},
        story_kind="dev",
        qa_context={},
        messages=[],
        dev_history=[],
        dev_progress=[],
        declared_unchanged={},
        tier1_gate={},
        gate_attempts=0,
        max_gate_attempts=3,
        gate_feedback=None,
        error=None,
        approved=False,
        rejected=False,
        merge_report={},
        truncated=False,
        clarification_questions=[],
        clarification_answers=[],
        attachment_paths=[],
        linked_issues_context=None,
        acceptance_criteria_hint=[],
        sdk_model=None,
        sdk_effort=None,
        project_id=None,
    )
