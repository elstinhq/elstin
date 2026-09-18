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
    # no PR at all (see forge/daemon/main.py's status mapping).
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
    # in, so forge/agents/roles/qa.py stays free of StateManager imports —
    # same separation the other roles keep.
    qa_context: dict[str, Any]
    messages: Annotated[list[dict[str, Any]], operator.add]
    # The dev loop's own forge/llm/types.py message history, JSON-serialized
    # (forge/llm/types.py:history_to_json/history_from_json) — durably
    # checkpointed by LangGraph's AsyncPostgresSaver alongside every other
    # field here, which is what lets a turn-capped run resume from exactly
    # where it stopped (see forge/agents/roles/dev_loop.py's module
    # docstring) instead of restarting cold. Replaces the old Claude Agent
    # SDK's dev_sdk_session_id, which asked the provider's own CLI process
    # to reload ITS server-side session — there is no equivalent
    # server-side session with a stateless Messages-API-shaped provider,
    # so forge owns this instead.
    dev_history: list[dict[str, Any]]
    dev_build_runner: dict[str, Any]
    error: str | None
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
    )
