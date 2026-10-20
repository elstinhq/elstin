"""Competing dev attempts: when one attempt keeps failing, try several.

A dev pass that fails the Tier 1 gate is sent back with the failures and tries to
fix what it wrote. That works when the code is nearly right; when the approach is
wrong, a second and third round of repairs on the same conversation rarely rescue
it, and the story fails after paying for all of them. Sampling the model again from
a clean start is a different bet, and the deterministic gate is exactly the
judge that bet needs: it costs nothing to run and says whether the code builds,
passes the plan's tests and covers the planned files.

So, once the gate has failed `dev_variants_after_gate_failures` times in a row and
`dev_variants` is 2 or more (off by default — it multiplies spend), the graph sends
the story here instead of back to the same conversation:

1. `count` scratch worktrees are made at the story branch's committed state, each
   with its dependencies installed, and dev runs in each at once from the approved
   plan. Each takes a share of what the story has left to spend, so together they
   stay inside `story_max_budget_usd`; each has its own file baselines; and each
   writes to the activity feed under a "variant N" label. A different approach hint
   is added for each so they are not the same attempt sampled again.
2. Each is judged by the gate, one at a time (builds are heavy). Passing the gate
   wins; then fewer blocking issues; then the smaller change; then the lower number.
3. The winner's files replace the story worktree's uncommitted state — the failed
   attempt goes — and the run goes back through the gate and the review in the real
   worktree, with the winner's own conversation as the dev history.
4. The scratch worktrees and their baselines are removed, whatever happened.

It runs at most once per run, and only where replacing the story worktree is safe:
see `not_eligible_reason`. Anything that goes wrong here — a worktree that will not
install, every attempt erroring — returns None and the run carries on exactly as it
would have without it. Judging is the gate's, not a model's: it picks the attempt
most likely to survive the review, not the best one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from elstin.agents.roles.dev_loop import run_dev
from elstin.agents.roles.verifier import run_deterministic_gate
from elstin.config.store import resolve_text, storage_dir
from elstin.services.memory_usage import measured
from elstin.services.run_activity import activity_label, append_activity
from elstin.tools.workspace import labels_in_plan
from elstin.tools.worktree import WorktreeError, create_scratch_worktree, discard_worktree, run_git, variant_worktree_path

logger = logging.getLogger(__name__)

MAX_VARIANTS = 4
DEFAULT_AFTER_FAILURES = 2

# One per attempt, cycled. A different angle on the same plan, not a different
# plan: the point is that the attempts are not the same sample twice.
APPROACH_HINTS: tuple[str, ...] = (
    "",
    "Approach for this attempt: prefer the smallest change that satisfies the plan, reusing what already exists.",
    "Approach for this attempt: find the closest existing implementation in this codebase and follow it as a template, structure and naming included.",
    "Approach for this attempt: start from the plan's own tests and acceptance criteria and work backwards from them.",
)


def variant_count(project_id: int | None) -> int:
    """How many competing attempts the project asks for, 1 meaning none."""
    try:
        wanted = int((resolve_text("dev_variants", project_id) or "1").strip())
    except ValueError:
        return 1
    return max(1, min(MAX_VARIANTS, wanted))


def failures_before_variants(project_id: int | None) -> int:
    try:
        wanted = int((resolve_text("dev_variants_after_gate_failures", project_id) or str(DEFAULT_AFTER_FAILURES)).strip())
    except ValueError:
        return DEFAULT_AFTER_FAILURES
    return max(1, wanted)


def not_eligible_reason(state: dict[str, Any]) -> str | None:
    """Why this run must not try competing attempts, or None when it may.
    Replacing the story worktree's uncommitted state with a winner's is only safe
    when that state is nothing but this run's own dev passes, which a fresh
    attempt regenerates."""
    if state.get("story_kind") == "qa":
        return "a QA story has no dev step"
    if state.get("variants_done"):
        return "competing attempts were already tried in this run"
    if not state.get("started_clean"):
        # An earlier run's uncommitted work is kept in the worktree on a retry
        # (elstin/daemon/main.py's _reusable_worktree); a scratch checkout at the
        # branch's commit would not have it, and adopting a winner would erase it.
        return "the story worktree already held uncommitted work from an earlier run"
    if state.get("integration_paths"):
        # Landing on the epic branch: the worktree holds a merge in progress.
        return "the story is being sent back to land on the epic branch"
    default_label = state.get("default_repo_label") or ""
    if len(labels_in_plan(state.get("plan") or {}, default_label)) > 1:
        return "the plan writes more than one repo"
    return None


@dataclass
class Attempt:
    index: int  # 1-based, the number in "variant N"
    worktree_path: str
    result: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    error: str | None = None
    hint: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.gate and self.gate.get("passed"))

    @property
    def blockers(self) -> int:
        gate = self.gate or {}
        return len(gate.get("required_fixes") or []) or len(gate.get("issues") or [])

    @property
    def files(self) -> int:
        return len((self.result or {}).get("file_changes") or [])


def rank_key(attempt: Attempt) -> tuple[int, int, int, int]:
    """Lower is better: passes the gate, then fewer blocking issues, then the
    smaller change, then the lower number (so a tie is stable)."""
    return (0 if attempt.passed else 1, attempt.blockers, attempt.files, attempt.index)


def choose(attempts: list[Attempt]) -> Attempt | None:
    """The best judged attempt, or None when none produced a result the gate saw."""
    judged = [a for a in attempts if a.result is not None and a.gate is not None]
    return min(judged, key=rank_key) if judged else None


def scoreboard(attempts: list[Attempt], winner: Attempt | None) -> str:
    lines = []
    for a in attempts:
        if a.error and a.result is None:
            outcome = f"failed: {a.error[:200]}"
        elif a.gate is None:
            outcome = "not judged"
        elif a.passed:
            outcome = f"passed the gate, {a.files} file(s)"
        else:
            outcome = f"failed the gate with {a.blockers} issue(s), {a.files} file(s)"
        lines.append(f"variant {a.index}{' (chosen)' if a is winner else ''}: {outcome}")
    return "\n".join(lines)


def adopt_changes(source: str, destination: str) -> list[str]:
    """Make `destination`'s uncommitted state exactly `source`'s: the destination
    goes back to its committed state, then every file the source added, changed or
    deleted is laid over it. Both are worktrees at the same commit. Returns the
    paths that changed. Ignored files (installed dependencies, build output) are
    left alone on both sides."""
    run_git(source, "add", "-A")  # scratch: staging it is harmless, and honours .gitignore and info/exclude
    listing = run_git(source, "diff", "--cached", "--no-renames", "--name-status", "-z", "HEAD").stdout
    tokens = listing.split("\0")
    entries = [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens) - 1, 2) if tokens[i]]

    run_git(destination, "reset", "--hard", "HEAD")
    run_git(destination, "clean", "-fd")
    root = os.path.realpath(destination)
    for status, relative in entries:
        target = os.path.realpath(os.path.join(destination, relative))
        if os.path.commonpath([root, target]) != root:
            raise WorktreeError(f"refusing to write outside the worktree: {relative}")
        if status.startswith("D"):
            if os.path.lexists(target):
                os.remove(target)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(source, relative), target)
    return [relative for _status, relative in entries]


async def run_dev_variants(
    *,
    count: int,
    plan: dict[str, Any],
    context_bundle: dict[str, Any],
    worktree_path: str,
    project_path: str,
    run_id: str,
    default_label: str,
    story_worktrees: dict[str, Any] | None = None,
    repo_roots: dict[str, str] | None = None,
    read_roots: dict[str, str] | None = None,
    read_only_labels: list[str] | None = None,
    story_base_refs: dict[str, str] | None = None,
    sdk_model: str | None = None,
    sdk_effort: str | None = None,
    project_id: int | None = None,
) -> dict[str, Any] | None:
    """Run `count` competing dev attempts and adopt the best into `worktree_path`.
    Returns the winner's `run_dev` result, with the story's own worktree entry put
    back and a `variants` summary, or None when nothing was adopted (the story
    worktree is then untouched)."""
    base_dir = storage_dir("worktrees_base_dir")
    data_dir = storage_dir("data_dir")
    story_entry = (story_worktrees or {}).get(default_label) or {
        "repo_path": (repo_roots or {}).get(default_label, project_path), "worktree_path": worktree_path, "branch": "",
    }
    attempts = [
        Attempt(i + 1, variant_worktree_path(base_dir, run_id, i + 1), hint=APPROACH_HINTS[i % len(APPROACH_HINTS)])
        for i in range(count)
    ]
    try:
        base = (await asyncio.to_thread(run_git, worktree_path, "rev-parse", "HEAD")).stdout.strip()
        made = await asyncio.gather(
            *(asyncio.to_thread(create_scratch_worktree, project_path, a.worktree_path, base) for a in attempts),
            return_exceptions=True,
        )
        for attempt, outcome in zip(attempts, made):
            if isinstance(outcome, BaseException):
                attempt.error = f"could not make its worktree: {outcome}"
        live = [a for a in attempts if a.error is None]
        if len(live) < 2:
            logger.warning("story %s: only %d scratch worktree(s) could be made — not comparing attempts", run_id, len(live))
            append_activity(
                run_id, type="status", phase="dev", title="Competing attempts skipped: too few scratch worktrees could be made",
                detail=scoreboard(attempts, None)[:2000],
            )
            return None

        append_activity(
            run_id, type="status", phase="dev", title=f"Trying {len(live)} competing attempts from the plan",
            detail="The gate has failed repeatedly, so instead of repairing the same attempt again, "
                   f"{len(live)} fresh ones run side by side and the gate picks the best.",
        )

        async def attempt_dev(attempt: Attempt) -> None:
            bundle = dict(context_bundle)
            if attempt.hint:
                bundle["project_context"] = f"{bundle.get('project_context') or ''}\n\n{attempt.hint}".strip()
            entry = {**story_entry, "worktree_path": attempt.worktree_path}
            with activity_label(f"variant {attempt.index}"):
                try:
                    attempt.result = await run_dev(
                        plan=plan, context_bundle=bundle, worktree_path=attempt.worktree_path, project_path=project_path,
                        run_id=run_id, repo_roots=repo_roots, read_roots=read_roots, story_base_refs=story_base_refs,
                        read_only_labels=read_only_labels, default_label=default_label,
                        story_worktrees={default_label: entry}, sdk_model=sdk_model, sdk_effort=sdk_effort,
                        project_id=project_id, baseline_key=f"{run_id}~v{attempt.index}", budget_share=1 / len(live),
                        read_guidance=False,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one attempt failing is a result, not the run's failure
                    attempt.error = str(exc) or type(exc).__name__
                    logger.warning("story %s: variant %d failed: %s", run_id, attempt.index, attempt.error)

        await asyncio.gather(*(attempt_dev(a) for a in live))

        # One at a time: each gate is a build, and several at once fight over the machine.
        for attempt in live:
            if attempt.result is None:
                continue
            try:
                attempt.gate = await measured(
                    run_id,
                    run_deterministic_gate(
                        plan=plan, worktree_path=attempt.worktree_path, file_changes=attempt.result.get("file_changes") or [],
                        project_path=project_path, run_id=None,
                        prior_build_runner=attempt.result.get("dev_build_runner") or None,
                        story_worktrees={default_label: {**story_entry, "worktree_path": attempt.worktree_path}},
                        default_label=default_label, declared_unchanged=attempt.result.get("declared_unchanged") or None,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                attempt.error = f"the gate could not judge it: {exc}"
                logger.warning("story %s: variant %d could not be judged: %s", run_id, attempt.index, exc)

        winner = choose(attempts)
        board = scoreboard(attempts, winner)
        if winner is None:
            append_activity(
                run_id, type="status", phase="dev", title="Competing attempts: none produced a result to adopt — carrying on",
                detail=board[:2000],
            )
            return None

        changed = await asyncio.to_thread(adopt_changes, winner.worktree_path, worktree_path)
        append_activity(
            run_id, type="status", phase="dev",
            title=f"Adopted variant {winner.index} ({'passed' if winner.passed else 'best of a failing set'}; {len(changed)} file(s))",
            detail=board[:2000], files=changed[:200],
        )
        adopted = dict(winner.result or {})
        adopted["story_worktrees"] = {**(story_worktrees or {}), default_label: story_entry}
        adopted["variants"] = [
            {"index": a.index, "passed": a.passed, "blockers": a.blockers, "files": a.files, "error": a.error, "chosen": a is winner}
            for a in attempts
        ]
        return adopted
    finally:
        # Synchronous on purpose: this runs on cancellation too, and must finish.
        for attempt in attempts:
            try:
                discard_worktree(project_path, attempt.worktree_path, None)
            except Exception:  # noqa: BLE001
                logger.warning("could not remove the scratch worktree %s", attempt.worktree_path, exc_info=True)
            shutil.rmtree(f"{data_dir}/baselines/{run_id}~v{attempt.index}", ignore_errors=True)
