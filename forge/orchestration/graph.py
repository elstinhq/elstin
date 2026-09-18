"""The LangGraph workflow. There are two paths through it, chosen at START
by the story's kind (see forge/services/story_kind.py):

    dev story:  planner -> [interrupt] -> dev -> verifier <-> dev -> END
    qa story:   qa -> END

They are disjoint on purpose. A QA story is a validation ticket: it has
nothing to plan and nothing to implement, so sending it through the planner
would produce a plan for work that isn't being asked for. Equally, a dev
story no longer ends in QA — QA is not a per-story stage. It runs once per
epic, as its own ticket, after every implementation story has merged (see
forge/agents/roles/qa.py's module docstring for why that is the only point
at which epic-wide validation is answerable).

Notable design points:

1. The checkpointer is Postgres (`langgraph-checkpoint-postgres`) instead of
   SQLite — this is what turns `interrupt_after` into durable suspend/wake:
   a run parked at the planner-approval gate survives a process restart and
   resumes from exactly where it left off via its `thread_id`, for free
   from LangGraph.
2. `interrupt_after=["planner"]` is not hardcoded at compile time — it's
   conditional on the `require_plan_approval` app_config gate (see
   forge/config/store.py), read once per compile.

The referenced-ticket enrichment planner_node could do via JiraService is
dropped here — Phase 1 doesn't wire that up yet (that lands with the JIRA
MCP server), so linked_issues_context passes through unchanged. Follow-up,
not a regression: nothing in Phase 1 populates it either.

forge is SDK-only (see forge/agents/roles/dev.py and planner.py's module
docstrings for why a hand-rolled chat-completion loop wasn't used), so
these node bodies call run_dev/run_planner directly.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from langgraph.graph import END, START, StateGraph

from forge.agents.roles.dev_loop import DevLoopError, run_dev
from forge.agents.roles.planner_loop import run_planner
from forge.agents.roles.qa import run_epic_qa
from forge.agents.roles.verifier import run_verifier
from forge.orchestration.state import FeatureRunState
from forge.services.run_activity import append_activity

logger = logging.getLogger("forge.graph")

DEFAULT_MAX_VERIFIER_ITERATIONS = 3


async def planner_node(state: FeatureRunState) -> dict:
    run_id = state.get("run_id", "")
    logger.info("planner starting run_id=%s", run_id)
    result = await run_planner(
        user_request=state["user_request"],
        project_path=state["project_path"],
        run_id=run_id or None,
        ticket_id=state.get("ticket_id"),
        attachment_paths=state.get("attachment_paths") or None,
        linked_issues_context=state.get("linked_issues_context"),
        acceptance_criteria_hint=state.get("acceptance_criteria_hint") or None,
        clarification_answers=state.get("clarification_answers") or None,
        sdk_model=state.get("sdk_model"),
        sdk_effort=state.get("sdk_effort"),
    )
    questions = result.get("questions") or []
    if questions:
        logger.info("planner awaiting clarification run_id=%s questions=%d", run_id, len(questions))
        return {
            "status": "awaiting_clarification",
            "context_bundle": result["context_bundle"],
            "plan": result["plan"],
            "clarification_questions": questions,
            "messages": result["messages"],
        }
    logger.info("planner success run_id=%s status=awaiting_approval", run_id)
    return {
        "status": "awaiting_approval",
        "context_bundle": result["context_bundle"],
        "plan": result["plan"],
        "acceptance_criteria": result["acceptance_criteria"],
        "messages": result["messages"],
    }


async def dev_node(state: FeatureRunState) -> dict:
    run_id = state.get("run_id", "")
    worktree_path = state.get("worktree_path")
    if not worktree_path:
        logger.error("dev failed run_id=%s reason=no_worktree", run_id)
        return {"status": "failed", "error": "Worktree not created", "messages": []}

    iteration = state.get("iteration", 0)
    if run_id:
        append_activity(run_id, type="status", phase="dev", title=f"Entering development (iteration {iteration})")

    try:
        result = await run_dev(
            plan=state.get("plan", {}),
            context_bundle=state.get("context_bundle", {}),
            worktree_path=worktree_path,
            project_path=state["project_path"],
            verifier_report=state.get("verifier_report") or None,
            run_id=run_id or None,
            # dev_history is this run's forge/llm/types.py message history,
            # JSON-serialized — durably checkpointed as part of this same
            # LangGraph state, so a turn-capped run resumes from exactly
            # where it stopped on the next retry. See forge/orchestration/
            # state.py's field docstring.
            prior_history=state.get("dev_history") or None,
            sdk_model=state.get("sdk_model"),
            sdk_effort=state.get("sdk_effort"),
        )
    except DevLoopError as exc:
        # Caught here, not left to propagate: nothing upstream of this node
        # (the daemon's poll loop included) catches per-story exceptions —
        # an uncaught one here would abort the whole poll cycle before it
        # reaches every epic after this one in the list, silently, forever
        # (see forge/daemon/main.py's poll_once). Returning status="failed"
        # instead routes through the same path every other expected
        # failure in this file already uses, which the daemon maps to a
        # real "failed" story a human can see and retry.
        logger.error("dev failed run_id=%s reason=provider_error: %s", run_id, exc)
        if run_id:
            append_activity(run_id, type="status", phase="dev", title="Development failed", detail=str(exc))
        return {"status": "failed", "error": str(exc), "messages": []}
    logger.info("dev success run_id=%s file_changes=%d", run_id, len(result.get("file_changes", [])))
    return {
        "status": "verifying",
        "file_changes": result.get("file_changes", []),
        "truncated": result.get("truncated", False),
        "messages": result.get("messages", []),
        "dev_history": result.get("dev_history", []),
        "dev_build_runner": result.get("dev_build_runner", {}),
    }


async def verifier_node(state: FeatureRunState) -> dict:
    run_id = state.get("run_id", "")
    worktree_path = state.get("worktree_path")
    if not worktree_path:
        logger.error("verifier failed run_id=%s reason=no_worktree", run_id)
        return {"status": "failed", "error": "Worktree not created", "messages": []}

    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations") or DEFAULT_MAX_VERIFIER_ITERATIONS
    if run_id:
        append_activity(
            run_id, type="status", phase="verifier",
            title=f"Entering verification (iteration {iteration + 1}/{max_iterations})",
        )

    result = await run_verifier(
        plan=state.get("plan", {}),
        acceptance_criteria=state.get("acceptance_criteria", []),
        worktree_path=worktree_path,
        file_changes=state.get("file_changes", []),
        project_path=state.get("project_path"),
        run_id=run_id or None,
        prior_build_runner=state.get("dev_build_runner") or None,
    )
    report = result["verifier_report"]

    if report.get("passed"):
        logger.info("verifier success run_id=%s -> completed", run_id)
        return {
            "status": "completed",
            "verifier_report": report,
            "diffs": result.get("diffs", []),
            "messages": result.get("messages", []),
        }

    iteration += 1
    if iteration >= max_iterations:
        logger.error("verifier exhausted run_id=%s iteration=%d/%d", run_id, iteration, max_iterations)
        return {
            "status": "failed",
            "iteration": iteration,
            "verifier_report": report,
            "diffs": result.get("diffs", []),
            "error": "Verifier retries exhausted",
            "messages": result.get("messages", []),
        }

    logger.warning("verifier retry run_id=%s next_iteration=%d/%d", run_id, iteration, max_iterations)
    return {
        "status": "developing",
        "iteration": iteration,
        "verifier_report": report,
        "diffs": result.get("diffs", []),
        "messages": result.get("messages", []),
    }


async def qa_node(state: FeatureRunState) -> dict:
    run_id = state.get("run_id", "")
    worktree_path = state.get("worktree_path")
    if not worktree_path:
        logger.error("qa failed run_id=%s reason=no_worktree", run_id)
        return {"status": "failed", "error": "Worktree not created", "messages": []}

    qa_context = state.get("qa_context") or {}
    if not qa_context.get("epic"):
        # The daemon is the only thing that can assemble this (it owns the
        # DB). Missing it means a QA story was dispatched through a path
        # that didn't populate it — fail loudly rather than run a review
        # with no acceptance criteria to review against.
        logger.error("qa failed run_id=%s reason=no_qa_context", run_id)
        return {"status": "failed", "error": "QA context not assembled", "messages": []}

    if run_id:
        append_activity(run_id, type="status", phase="qa", title="Entering epic QA")

    result = await run_epic_qa(
        qa_story=qa_context.get("qa_story", {}),
        epic=qa_context["epic"],
        dev_stories=qa_context.get("dev_stories", []),
        branch_diff=qa_context.get("branch_diff", {}),
        worktree_path=worktree_path,
        project_path=state["project_path"],
        run_id=run_id or None,
    )
    report = result.get("qa_report", {})

    # A failing full test suite on the merged branch is deterministic
    # evidence that the epic is not done, so it fails the run. The review's
    # own subjective verdict does NOT fail it — those findings are reported
    # to the QA ticket for a human to weigh, because halting an epic on a
    # model's opinion is not a call forge should make by itself.
    suite = report.get("full_test_suite") or {}
    if suite.get("ran") and not suite.get("passed"):
        logger.error("qa run_id=%s full test suite failed on merged branch", run_id)
        return {
            "status": "failed",
            "qa_report": report,
            "error": "Full test suite failed on the integration branch",
            "messages": result.get("messages", []),
        }

    logger.info("qa success run_id=%s verdict=%s", run_id, report.get("verdict"))
    return {
        "status": "qa_completed",
        "qa_report": report,
        "messages": result.get("messages", []),
    }


def route_entry(state: FeatureRunState) -> str:
    """The dev/qa fork. Defaults to the dev path when unset so a run
    checkpointed before story kinds existed resumes the way it started."""
    return "qa" if state.get("story_kind") == "qa" else "planner"


def route_after_dev(state: FeatureRunState) -> str:
    # dev_node returns status="failed" both for its pre-existing defensive
    # checks (no worktree) and for a caught DevLoopError — either
    # way, sending that state into verifier_node anyway would silently
    # overwrite it with whatever the verifier decides about an untouched or
    # partial worktree, discarding the real failure reason. This was
    # previously unreachable in practice: a dev provider error used to
    # propagate as an uncaught exception rather than a "failed" status, so
    # the graph itself never actually got a chance to route on it.
    if state.get("status") == "failed":
        return END
    return "verifier"


def route_after_verifier(state: FeatureRunState) -> str:
    if state.get("status") == "developing":
        return "dev"
    return END


def build_graph() -> StateGraph:
    graph = StateGraph(FeatureRunState)
    graph.add_node("planner", planner_node)
    graph.add_node("dev", dev_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("qa", qa_node)

    graph.add_conditional_edges(START, route_entry, {"planner": "planner", "qa": "qa"})
    graph.add_edge("planner", "dev")
    graph.add_conditional_edges("dev", route_after_dev, {"verifier": "verifier", END: END})
    graph.add_conditional_edges("verifier", route_after_verifier, {"dev": "dev", END: END})
    graph.add_edge("qa", END)
    return graph


@asynccontextmanager
async def checkpointer_from_url(database_url: str):
    """Postgres checkpointer — the durable-suspend/wake mechanism (see this
    module's docstring). `setup()` creates its tables on first use, same as
    alembic upgrade head does for forge's own tables, just LangGraph-owned.

    Accepts the same `DATABASE_URL` used everywhere else in the app
    (SQLAlchemy's `postgresql+psycopg://` form) and strips the `+psycopg`
    driver suffix — `AsyncPostgresSaver` wants a plain psycopg conninfo
    (`postgresql://...`), not a SQLAlchemy dialect URL.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
        await saver.setup()
        yield saver


def compile_graph(checkpointer, *, require_plan_approval: bool):
    builder = build_graph()
    interrupt_after = ["planner"] if require_plan_approval else []
    return builder.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)
