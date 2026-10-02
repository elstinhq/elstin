"""The LangGraph workflow. There are two paths through it, chosen at START
by the story's kind (see forge/services/story_kind.py):

    dev story:  planner -> [interrupt] -> dev -> gate -> verifier -> END
                                           ^______|  (gate failed)
                                           ^_______________|  (review failed)
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
3. **Tier 1 fast-fail (`gate_node`).** Every dev pass is followed by the
   deterministic gate — planned-file presence, compile/lint, the plan's
   tests, all subprocesses, no model (forge/agents/roles/verifier.py's
   run_deterministic_gate) — *before* the verifier node. A worktree that
   doesn't build never reaches the LLM reviewer: the gate's toolchain
   output is formatted into one targeted user message (format_gate_feedback)
   and the graph routes straight back to dev, which appends it to the
   run's existing history so the model keeps everything it already read.
   Three consecutive gate failures fail the run (max_gate_attempts).
   Only a worktree that clears Tier 1 reaches verifier_node, which then
   reuses the gate report instead of re-running the toolchain. This is
   what makes cheap/non-frontier models workable: their most common
   failure is code that doesn't compile, and the old path spent a full
   verifier iteration (and, on the LLM-review half, real money) finding
   that out.

The referenced-ticket enrichment planner_node could do via JiraService is
dropped here — Phase 1 doesn't wire that up yet (that lands with the JIRA
MCP server), so linked_issues_context passes through unchanged. Follow-up,
not a regression: nothing in Phase 1 populates it either.

forge is SDK-only (see forge/agents/roles/dev.py and planner.py's module
docstrings for why a hand-rolled chat-completion loop wasn't used), so
these node bodies call run_dev/run_planner directly.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from langgraph.graph import END, START, StateGraph

from forge.agents.roles.dev_loop import DevLoopError, run_dev
from forge.agents.roles.dev_variants import (
    failures_before_variants, not_eligible_reason, run_dev_variants, variant_count,
)
from forge.agents.roles.planner_loop import PlannerLoopError, run_planner
from forge.agents.roles.qa import run_epic_qa
from forge.agents.roles.verifier import format_gate_feedback, run_deterministic_gate, run_verifier
from forge.orchestration.state import FeatureRunState
from forge.services.generation import GenerationError
from forge.services.memory_usage import measured
from forge.services.run_activity import append_activity
from forge.services.usage import record_run_outcome

logger = logging.getLogger("forge.graph")

DEFAULT_MAX_VERIFIER_ITERATIONS = 3
# Consecutive Tier 1 gate failures before the run fails outright. Separate
# from the verifier's iteration budget: a gate retry costs no model call
# for review, so it gets its own, and a gate pass resets it.
DEFAULT_MAX_GATE_ATTEMPTS = 3


def _limit_state(exc: BaseException) -> dict | None:
    """The provider limit a role's error carries, as run state (see
    FeatureRunState.provider_limit), or None for any other failure."""
    limit = getattr(exc, "limit", None)
    return limit.as_state() if limit is not None else None


async def planner_node(state: FeatureRunState) -> dict:
    run_id = state.get("run_id", "")
    logger.info("planner starting run_id=%s", run_id)
    try:
        # Planning reads the worktree and the epic branch, not the clones
        # (FeatureRunState.read_roots); a run checkpointed before read_roots
        # existed falls back to the clones as before.
        read_roots = state.get("read_roots") or {}
        default_label = state.get("default_repo_label") or ""
        result = await run_planner(
            user_request=state["user_request"],
            project_path=read_roots.get(default_label) or state["project_path"],
            run_id=run_id or None,
            ticket_id=state.get("ticket_id"),
            attachment_paths=state.get("attachment_paths") or None,
            linked_issues_context=state.get("linked_issues_context"),
            acceptance_criteria_hint=state.get("acceptance_criteria_hint") or None,
            clarification_answers=state.get("clarification_answers") or None,
            # Answers are either this run's round trip (the planner asked and
            # is resumed) or a person's earlier decisions carried into a
            # fresh run (forge/daemon/main.py) — which re-explores and may
            # still ask about something they do not settle.
            answers_from_earlier_run=state.get("status") != "awaiting_clarification",
            repo_roots=read_roots or state.get("repo_roots") or None,
            default_label=default_label,
            project_guide=state.get("project_guide") or None,
            prior_work=state.get("prior_work") or None,
            read_only_labels=state.get("read_only_labels") or None,
            sdk_model=state.get("sdk_model"),
            sdk_effort=state.get("sdk_effort"),
            project_id=state.get("project_id"),
        )
    except PlannerLoopError as exc:
        # Same reasoning as dev_node's DevLoopError catch below, plus one
        # specific to this node: an exception here propagates out of the
        # run, and the daemon's unwind restores the story to its PREVIOUS
        # status — "pending" — which the next poll dispatches again. A
        # planner that cannot produce a usable plan therefore re-planned
        # every cycle, paying for the same failure forever. Ending the run
        # "failed" stops that: a human sees it, and Retry is a deliberate
        # act rather than the default.
        logger.error("planner failed run_id=%s: %s", run_id, exc)
        if run_id:
            append_activity(run_id, type="status", phase="planner", title="Planning failed", detail=str(exc)[:1000])
        return {"status": "failed", "error": str(exc), "messages": [], "provider_limit": _limit_state(exc)}
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
    gate_feedback = state.get("gate_feedback") or None
    if run_id:
        append_activity(
            run_id, type="status", phase="dev",
            title=f"Entering development (iteration {iteration}" + (", fixing deterministic-gate failures)" if gate_feedback else ")"),
        )

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
            prior_progress=state.get("dev_progress") or None,
            prior_declared=state.get("declared_unchanged") or None,
            # Set by gate_node when Tier 1 just failed: the targeted fix
            # message becomes this iteration's brief (see point 3 in the
            # module docstring). Takes precedence over verifier_report,
            # which may be stale from an earlier review round.
            gate_feedback=gate_feedback,
            integration_paths=state.get("integration_paths") or None,
            repo_roots=state.get("repo_roots") or None,
            read_roots=state.get("read_roots") or None,
            story_base_refs=state.get("story_base_refs") or None,
            read_only_labels=state.get("read_only_labels") or None,
            default_label=state.get("default_repo_label") or "",
            story_worktrees=state.get("story_worktrees") or None,
            sdk_model=state.get("sdk_model"),
            sdk_effort=state.get("sdk_effort"),
            project_id=state.get("project_id"),
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
        return {
            "status": "failed", "error": str(exc), "messages": [], "gate_feedback": None,
            "provider_limit": _limit_state(exc),
        }
    logger.info("dev success run_id=%s file_changes=%d", run_id, len(result.get("file_changes", [])))
    return {
        "status": "verifying",
        "file_changes": result.get("file_changes", []),
        "truncated": result.get("truncated", False),
        "messages": result.get("messages", []),
        "dev_history": result.get("dev_history", []),
        "dev_progress": result.get("dev_progress", []),
        "declared_unchanged": result.get("declared_unchanged", {}),
        # Grows when the plan turned out to write a repo that had no
        # worktree yet; the daemon records it after the run.
        "story_worktrees": result.get("story_worktrees", {}),
        "dev_build_runner": result.get("dev_build_runner", {}),
        # Consumed — a later gate failure produces a fresh one.
        "gate_feedback": None,
    }


async def gate_node(state: FeatureRunState) -> dict:
    """Tier 1 deterministic gate — see point 3 in the module docstring.
    Never calls a model: pass -> verifier, fail -> back to dev with a
    targeted fix message, too many consecutive fails -> the run fails."""
    run_id = state.get("run_id", "")
    worktree_path = state.get("worktree_path")
    if not worktree_path:
        logger.error("gate failed run_id=%s reason=no_worktree", run_id)
        return {"status": "failed", "error": "Worktree not created", "messages": []}

    attempts = state.get("gate_attempts", 0)
    max_attempts = state.get("max_gate_attempts") or DEFAULT_MAX_GATE_ATTEMPTS

    gate = await measured(
        run_id or None,
        run_deterministic_gate(
            plan=state.get("plan", {}),
            worktree_path=worktree_path,
            file_changes=state.get("file_changes", []),
            project_path=state.get("project_path"),
            run_id=run_id or None,
            prior_build_runner=state.get("dev_build_runner") or None,
            story_worktrees=state.get("story_worktrees") or None,
            default_label=state.get("default_repo_label") or "",
            declared_unchanged=state.get("declared_unchanged") or None,
        ),
    )

    record_run_outcome(run_id, gate_runs=1, gate_failures=0 if gate.get("passed") else 1)

    # The build environment, not the code (LanguageProfile.environment_problem):
    # the developer step cannot fix a toolchain, and sent back it "fixes"
    # working code trying — MKT2-866 lost three passes to a JDK. Stop, say why.
    environment = [issue["environment"] for issue in gate.get("issues") or [] if isinstance(issue, dict) and issue.get("environment")]
    if environment:
        reason = "; ".join(dict.fromkeys(environment))
        logger.error("gate environment problem run_id=%s: %s", run_id, reason)
        if run_id:
            append_activity(run_id, type="status", phase="verifier", title="Build environment problem — stopping, the code is not at fault", detail=reason[:4000])
        return {
            "status": "failed",
            "tier1_gate": gate,
            "gate_attempts": attempts,
            "verifier_report": gate,
            "gate_feedback": None,
            "error": f"Build environment problem: {reason}",
            "messages": [{"role": "gate", "content": "The build environment failed, not the code; stopped."}],
        }

    if gate.get("passed"):
        logger.info("gate passed run_id=%s -> verifier", run_id)
        return {"status": "verifying", "tier1_gate": gate, "gate_attempts": 0, "gate_feedback": None}

    attempts += 1
    blockers = gate.get("required_fixes") or [i.get("reason", "") for i in gate.get("issues", []) if isinstance(i, dict)]
    if attempts >= max_attempts:
        logger.error("gate exhausted run_id=%s attempts=%d/%d: %s", run_id, attempts, max_attempts, "; ".join(map(str, blockers[:5])))
        if run_id:
            append_activity(run_id, type="status", phase="verifier", title=f"Deterministic gate failed {attempts} time(s) in a row — giving up", detail="; ".join(map(str, blockers[:5])))
        return {
            "status": "failed",
            "tier1_gate": gate,
            "gate_attempts": attempts,
            # Same shape run_verifier used to return when its own gate
            # failed, so the daemon's coverage-gap/report readers see a
            # familiar verifier_report on this failure path too.
            "verifier_report": gate,
            "gate_feedback": None,
            "error": f"Deterministic gate failed after {attempts} fix attempt(s): " + "; ".join(map(str, blockers[:3])),
            "messages": [{"role": "gate", "content": "Deterministic gate failed; retries exhausted."}],
        }

    # file_changes is git's view of every worktree the story writes, so empty
    # means nothing on disk has changed — not merely that dev claims nothing.
    feedback = format_gate_feedback(
        gate, attempt=attempts, max_attempts=max_attempts, wrote_nothing=not state.get("file_changes"),
    )
    # Repairing the same attempt a third time rarely rescues a wrong approach.
    # When the project asks for it (dev_variants, off by default), several fresh
    # attempts run side by side instead, and the gate picks the best.
    project_id = state.get("project_id")
    compete = (
        variant_count(project_id) >= 2
        and attempts == failures_before_variants(project_id)
        and not_eligible_reason(state) is None
    )
    logger.warning(
        "gate failed run_id=%s attempt=%d/%d -> %s: %s", run_id, attempts, max_attempts,
        "competing attempts" if compete else "dev", "; ".join(map(str, blockers[:5])),
    )
    return {
        "status": "developing",
        "tier1_gate": gate,
        "gate_attempts": attempts,
        "gate_feedback": feedback,
        "variants_pending": compete,
        "messages": [{"role": "gate", "content": f"Deterministic gate failed ({len(blockers)} blocker(s)); sent back to dev."}],
    }


async def variants_node(state: FeatureRunState) -> dict:
    """Competing dev attempts after repeated gate failures (forge/agents/roles/
    dev_variants.py). Returns to the gate with the winner adopted into the story
    worktree, or — whenever anything goes wrong or nothing is adopted — to dev
    exactly as the run would have gone without it. Never fails the run."""
    run_id = state.get("run_id", "")
    done = {"variants_pending": False, "variants_done": True}
    reason = not_eligible_reason(state)
    if reason:
        logger.info("competing attempts skipped run_id=%s: %s", run_id, reason)
        return done
    project_id = state.get("project_id")
    try:
        outcome = await run_dev_variants(
            count=variant_count(project_id),
            plan=state.get("plan", {}),
            context_bundle=state.get("context_bundle", {}),
            worktree_path=state["worktree_path"],
            project_path=state["project_path"],
            run_id=run_id,
            default_label=state.get("default_repo_label") or "",
            story_worktrees=state.get("story_worktrees") or None,
            repo_roots=state.get("repo_roots") or None,
            read_roots=state.get("read_roots") or None,
            read_only_labels=state.get("read_only_labels") or None,
            story_base_refs=state.get("story_base_refs") or None,
            sdk_model=state.get("sdk_model"),
            sdk_effort=state.get("sdk_effort"),
            project_id=project_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a saving that failed is never a reason to fail the run
        logger.exception("competing attempts failed run_id=%s", run_id)
        if run_id:
            append_activity(run_id, type="status", phase="dev", title="Competing attempts failed — carrying on with the same attempt", detail=str(exc)[:1000])
        return done
    if outcome is None:
        return done
    return {
        **done,
        "status": "verifying",
        "file_changes": outcome.get("file_changes", []),
        "truncated": outcome.get("truncated", False),
        "messages": outcome.get("messages", []),
        "dev_history": outcome.get("dev_history", []),
        "dev_progress": outcome.get("dev_progress", []),
        "declared_unchanged": outcome.get("declared_unchanged", {}),
        "story_worktrees": outcome.get("story_worktrees", {}),
        "dev_build_runner": outcome.get("dev_build_runner", {}),
        # The winner earned a look, not a free pass: one more failure sends it
        # back to dev once, a second fails the run (DEFAULT_MAX_GATE_ATTEMPTS).
        "gate_attempts": 1,
        "gate_feedback": None,
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

    # Counted before the call, so an invocation that fails to produce a
    # usable answer still appears — that rate is exactly what judges a
    # cheap model in this slot, and it is the one case a post-call record
    # would omit (see forge/db/models/run_outcome.py).
    record_run_outcome(run_id, verifier_runs=1)

    try:
        result = await run_verifier(
            plan=state.get("plan", {}),
            acceptance_criteria=state.get("acceptance_criteria", []),
            worktree_path=worktree_path,
            file_changes=state.get("file_changes", []),
            project_path=state.get("project_path"),
            run_id=run_id or None,
            prior_build_runner=state.get("dev_build_runner") or None,
            story_worktrees=state.get("story_worktrees") or None,
            default_label=state.get("default_repo_label") or "",
            declared_unchanged=state.get("declared_unchanged") or None,
            # gate_node already ran the toolchain on this exact worktree state
            # — reuse its report rather than paying for a second run.
            precomputed_gate=state.get("tier1_gate") or None,
            project_id=state.get("project_id"),
        )
    except GenerationError as exc:
        # Same reasoning as dev_node's DevLoopError catch: an exception
        # escaping this node reaches the daemon's poll loop, which has no
        # per-story handler, so it abandons every epic queued behind this
        # one for the rest of the cycle. A model that answered with prose
        # instead of JSON, or a provider that was down for this one call,
        # is a failed story a human can retry — not a reason to stall the
        # queue. The Tier 1 gate has already passed at this point, so the
        # worktree does build; only the subjective review is missing.
        logger.error("verifier failed run_id=%s reason=generation_error: %s", run_id, exc)
        if run_id:
            append_activity(run_id, type="status", phase="verifier", title="Verification failed", detail=str(exc)[:1000])
        return {
            "status": "failed", "error": f"Verifier review could not be completed: {exc}", "messages": [],
            "provider_limit": _limit_state(exc),
        }
    report = result["verifier_report"]
    # A rejection here is a build that cleared Tier 1 and the reviewer
    # still judged incomplete — the counter that says whether the gate is
    # checking the right things.
    record_run_outcome(run_id, verifier_rejections=0 if report.get("passed") else 1)

    if report.get("passed"):
        logger.info("verifier success run_id=%s -> completed", run_id)
        return {
            "status": "completed",
            "verifier_report": report,
            "diffs": result.get("diffs", []),
            "messages": result.get("messages", []),
        }

    # Sending this back to dev would buy three more no-op passes at model
    # prices: dev may only write files the plan names, and this rejection
    # means the plan names none. The fix is a new plan, which only a
    # redispatch produces.
    if report.get("nothing_to_verify"):
        logger.error("verifier run_id=%s has nothing to verify — failing without a dev retry", run_id)
        return {
            "status": "failed",
            "verifier_report": report,
            "diffs": result.get("diffs", []),
            "error": (report.get("required_fixes") or ["Nothing to verify"])[0],
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

    try:
        result = await measured(
            run_id or None,
            run_epic_qa(
                qa_story=qa_context.get("qa_story", {}),
                epic=qa_context["epic"],
                dev_stories=qa_context.get("dev_stories", []),
                branch_diff=qa_context.get("branch_diff", {}),
                other_repo_diffs=qa_context.get("other_repo_diffs") or None,
                worktree_path=worktree_path,
                project_path=state["project_path"],
                run_id=run_id or None,
                project_id=state.get("project_id"),
            ),
        )
    except GenerationError as exc:
        # Same reasoning as verifier_node's catch above.
        logger.error("qa failed run_id=%s reason=generation_error: %s", run_id, exc)
        if run_id:
            append_activity(run_id, type="status", phase="qa", title="Epic QA failed", detail=str(exc)[:1000])
        return {
            "status": "failed", "error": f"Epic QA review could not be completed: {exc}", "messages": [],
            "provider_limit": _limit_state(exc),
        }
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


def route_after_planner(state: FeatureRunState) -> str:
    # planner_node returns status="failed" for a PlannerLoopError, which
    # includes a plan the plan gate rejected on every attempt. Without this
    # the unconditional planner->dev edge sent that failure on to dev,
    # which would be scoped to write the files of a plan that does not
    # exist.
    if state.get("status") == "failed":
        return END
    # A clarification round-trip's "plan" is a placeholder until a human
    # answers (planner_node) — status stays "awaiting_clarification" until
    # planner actually runs again with an answer. interrupt_after=["planner"]
    # computes this edge once, right when planner first returns that status,
    # and freezes it into the checkpoint's `next` pointer; a later resume
    # (forge/daemon/main.py patches in clarification_answers via
    # `aupdate_state`, which re-evaluates this same edge against the patched
    # state) must land back on "planner" to actually finish planning, not on
    # "dev", which would be scoped to write the files of a plan that was
    # never finished — see MKT2-858, where this sent an unanswered-shaped
    # plan straight to dev and the run failed with "nothing to verify".
    if state.get("status") == "awaiting_clarification":
        return "planner"
    return "dev"


def route_after_dev(state: FeatureRunState) -> str:
    # dev_node returns status="failed" both for its pre-existing defensive
    # checks (no worktree) and for a caught DevLoopError — either
    # way, sending that state on anyway would silently overwrite it with
    # whatever the gate/verifier decides about an untouched or partial
    # worktree, discarding the real failure reason.
    if state.get("status") == "failed":
        return END
    return "gate"


def route_after_gate(state: FeatureRunState) -> str:
    status = state.get("status")
    if status == "developing":
        return "variants" if state.get("variants_pending") else "dev"
    if status == "failed":
        return END
    return "verifier"


def route_after_variants(state: FeatureRunState) -> str:
    # An adopted winner goes through the gate in the real worktree; nothing
    # adopted goes back to dev with the gate's feedback, as it would have.
    return "gate" if state.get("status") == "verifying" else "dev"


def route_after_verifier(state: FeatureRunState) -> str:
    if state.get("status") == "developing":
        return "dev"
    return END


def build_graph() -> StateGraph:
    graph = StateGraph(FeatureRunState)
    graph.add_node("planner", planner_node)
    graph.add_node("dev", dev_node)
    graph.add_node("gate", gate_node)
    graph.add_node("variants", variants_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("qa", qa_node)

    graph.add_conditional_edges(START, route_entry, {"planner": "planner", "qa": "qa"})
    graph.add_conditional_edges("planner", route_after_planner, {"planner": "planner", "dev": "dev", END: END})
    graph.add_conditional_edges("dev", route_after_dev, {"gate": "gate", END: END})
    graph.add_conditional_edges(
        "gate", route_after_gate, {"verifier": "verifier", "dev": "dev", "variants": "variants", END: END},
    )
    graph.add_conditional_edges("variants", route_after_variants, {"gate": "gate", "dev": "dev"})
    graph.add_conditional_edges("verifier", route_after_verifier, {"dev": "dev", END: END})
    graph.add_edge("qa", END)
    return graph


# setup() runs one caller at a time. Stories run in parallel
# (forge/daemon/main.py), and on a fresh database two runs starting together
# both created LangGraph's tables — Postgres rejects the second CREATE of the
# same type, failing that run. It still runs on every open rather than once
# per process: it is a cheap no-op on existing tables, and it recreates them
# if the schema was reset underneath a running process. Locks are per event
# loop: an asyncio lock belongs to the loop it was first used on.
_checkpointer_setup_locks: dict[tuple[int, str], asyncio.Lock] = {}


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
        async with _checkpointer_setup_locks.setdefault((id(asyncio.get_running_loop()), conninfo), asyncio.Lock()):
            await saver.setup()
        yield saver


def compile_graph(checkpointer, *, require_plan_approval: bool):
    builder = build_graph()
    interrupt_after = ["planner"] if require_plan_approval else []
    return builder.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)
