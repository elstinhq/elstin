"""The daemon's half of the prior-work lookup: which tickets to look up and
when, where the answer is kept, and what the planner is told.

The git queries themselves are elstin/services/prior_work.py. This module
decides:

- **When.** `poll_once` calls `look_up_prior_work` for every active epic on
  every pass, before anything is dispatched — and in manual-run projects
  too, so what was found shows up as soon as the epic is added, not when
  someone presses Start. It returns at once when there is nothing to look
  up. A story is looked up exactly once: while it is still pending and has
  no branch. The epic's own key is looked up until the epic branch exists.
  First dispatch calls it again for the one story being started, in case
  Start was pressed before a poll got to it.
- **What the planner reads.** `planner_prior_work` turns a stored lookup
  into the `prior_work` block the planner prompt already had a slot for
  (elstin/agents/roles/planner.py's _format_prior_work_block): which branch
  this story's worktree starts from, what is only reference, what is
  already in the default branch.

Where the branches start from what was found is the daemon's first dispatch
(elstin/daemon/main.py's _execute_story_run).
"""
from __future__ import annotations

import logging

from elstin.orchestration.integration import integration_branch_name
from elstin.services.github_service import GitHubService, parse_github_remote
from elstin.services.prior_work import carry_picks, is_searchable_key, lookup_due, now_iso, start_candidate, survey_repo
from elstin.services.run_activity import append_activity
from elstin.state.manager import StateManager
from elstin.tools.workspace import build_repo_roots
from elstin.tools.worktree import branch_name_for, run_git

logger = logging.getLogger("elstin.daemon.prior_work")

# How much of a reference-only branch's diff the planner is shown — the
# code it may want to port. A branch the worktree starts from needs none:
# its code is on disk.
_DIFF_SAMPLE_CHARS = 2500
_MAX_PR_LOOKUPS_PER_REPO = 5


def look_up_prior_work(sm: StateManager, epic: dict, *, story_keys: list[str] | None = None) -> list[str]:
    """Look up and store what already exists for the epic's not-yet-started
    stories (or just `story_keys`), and for the epic itself until its branch
    exists. Returns the keys it looked up — empty when there was nothing to
    do, which is the common case once an epic is underway."""
    epic_key = epic["jira_key"]
    repos = sm.list_project_repos(epic["project_id"]) if epic.get("project_id") else []
    if not repos:
        return []
    if story_keys is None:
        # Tickets closed in the source too: "Done" is not "in the default
        # branch", and a done ticket's branch is work the epic builds on
        # (elstin/daemon/main.py's _bring_done_work_into_epic).
        stories = [
            s for s in sm.list_stories_for_epic(epic_key)
            if not s.get("parent_key") and not s.get("branch") and lookup_due(s.get("prior_work"))
            and (s["status"] == "pending" or (s["status"] == "skipped" and s.get("parked_reason") == "closed_externally"))
        ]
    else:
        stories = [
            s for s in (sm.get_story(key) for key in story_keys)
            if s is not None and not s.get("branch") and lookup_due(s.get("prior_work"))
        ]
    look_at_epic = lookup_due(epic.get("prior_work")) and not epic.get("integration_branch")
    if not stories and not look_at_epic:
        return []

    # Labels from every folder — they must match the run's — but only the
    # repos a story can write are looked in: work cannot continue in a
    # read-only repo, and a reference folder is not a repo.
    _default_label, all_roots = build_repo_roots(repos, epic.get("repo"))
    writable = {repo["path"] for repo in repos if repo.get("kind", "repo") == "repo" and not repo.get("read_only")}
    roots = {label: path for label, path in all_roots.items() if path in writable}
    keys: dict[str, list[str]] = {}
    # Elstin's own names are not prior work: a story branch elstin would make
    # (it could exist from an earlier run of this story), and the epic branch.
    exclude = {
        epic.get("integration_branch") or integration_branch_name(
            epic_key, external_source=epic.get("external_source"), external_id=epic.get("external_id"),
        )
    }
    for story in stories:
        keys[story["jira_key"]] = [story["jira_key"], *(t["jira_key"] for t in sm.list_subtasks(story["jira_key"]))]
        exclude.add(branch_name_for(
            story["jira_key"], story.get("summary") or story["jira_key"],
            issue_type=story.get("issue_type"), labels=story.get("labels"),
            external_source=story.get("external_source"), external_id=story.get("external_id"),
        ))
    if look_at_epic:
        keys[epic_key] = [epic_key]

    searchable = {target: [k for k in aliases if is_searchable_key(k)] for target, aliases in keys.items()}
    targets = {target: aliases for target, aliases in searchable.items() if aliases}
    looked_at = now_iso()
    results = {
        target: {"looked_at": looked_at, "applicable": bool(searchable[target]), "keys": keys[target], "repos": {}, "errors": {}}
        for target in keys
    }
    for label, path in roots.items():
        if not targets:
            break
        try:
            findings = survey_repo(path, targets, exclude_branches=exclude)
        except Exception as exc:  # noqa: BLE001 — one unreadable repo must not hide what the others show
            logger.warning("epic %s: could not look up prior work in %s: %s", epic_key, path, exc)
            for target in targets:
                results[target]["errors"][label] = str(exc)[:300]
            continue
        for target, finding in findings.items():
            if finding["verdict"] != "none":
                _attach_pull_requests(path, finding)
                results[target]["repos"][label] = finding

    for story in stories:
        result = results[story["jira_key"]]
        picks = carry_picks(story.get("prior_work"), result["repos"])  # a "Look again" keeps what still applies
        if picks:
            result["picks"] = picks
        if (story.get("prior_work") or {}).get("epic_merge"):
            result["epic_merge"] = story["prior_work"]["epic_merge"]  # what already happened stays recorded
        sm.set_story_prior_work(story["jira_key"], result)
        _announce(story["jira_key"], result)
    if look_at_epic:
        sm.set_epic_prior_work(epic_key, results[epic_key])
    found = sorted(target for target, result in results.items() if result["repos"])
    logger.info("epic %s: looked up prior work for %d ticket(s); found some for %s", epic_key, len(results), found or "none")
    return sorted(results)


def _attach_pull_requests(repo_path: str, finding: dict) -> None:
    """Best effort: the pull request each unmerged branch already has, when
    GitHub is configured. Display only — nothing decides on it."""
    if not GitHubService.is_configured():
        return
    remote = parse_github_remote(repo_path)
    if remote is None:
        return
    owner, name = remote
    try:
        service = GitHubService()
    except Exception:  # noqa: BLE001
        return
    for candidate in [c for c in finding["candidates"] if not c["in_default"]][:_MAX_PR_LOOKUPS_PER_REPO]:
        branch = candidate["branch"].removeprefix("origin/")
        try:
            prs = service.find_pull_requests(owner, name, branch)
        except Exception as exc:  # noqa: BLE001
            logger.info("could not look up pull requests for %s in %s/%s: %s", branch, owner, name, exc)
            return
        if prs:
            candidate["pull_request"] = prs[0]


def _announce(story_key: str, result: dict) -> None:
    lines = []
    for label, finding in sorted(result["repos"].items()):
        if finding["verdict"] == "continue":
            chosen = next(c for c in finding["candidates"] if c["branch"] == finding["chosen"])
            lines.append(f"{label}: will continue from {chosen['branch']} ({chosen['ahead']} commit(s) not in {finding['default_branch']})")
        elif finding["verdict"] == "choose":
            names = ", ".join(c["branch"] for c in finding["candidates"] if not c["in_default"])
            lines.append(f"{label}: several branches have diverged work ({names}) — pick one to continue from")
        else:
            lines.append(f"{label}: work for this ticket is already in {finding['default_branch']}")
    if lines:
        append_activity(
            story_key, type="status", phase="system", title="Found work already done on this ticket", detail="\n".join(lines),
        )


def planner_prior_work(story: dict, epic: dict, repo_roots: dict[str, str], default_label: str) -> dict:
    """The planner's view of what was found: the `prior_work` block its
    prompt reserves (elstin/agents/roles/planner.py's _format_prior_work_block).

    Each candidate says whether its code is ON DISK in the worktree being
    planned — the branch this story's worktree starts from, and one the epic
    branch started from — or only reference, with a diff sample to port
    from. Work already in the default branch is in the worktree too; the
    planner is told so it checks what, if anything, is still missing rather
    than planning the ticket again."""
    candidates: list[dict] = []
    merged_commits: dict[str, list[str]] = {}
    for source, lookup, reason in (("story", story.get("prior_work"), "branch_name"), ("epic", epic.get("prior_work"), "epic_branch")):
        for label, finding in sorted(((lookup or {}).get("repos") or {}).items()):
            start = start_candidate(lookup, label)
            display = label if label != default_label else f"{label} (default repo)"
            for candidate in finding.get("candidates") or []:
                selected = start is not None and candidate["branch"] == start["branch"]
                entry = {
                    "repo": display,
                    "branch": candidate["branch"],
                    "match_reason": reason,
                    "selected": selected,
                    "status": (
                        f"already in {finding['default_branch']}" if candidate["in_default"]
                        else f"{candidate['ahead']} commit(s) not in {finding['default_branch']}"
                    ),
                    "commits": candidate.get("commits") or [],
                    "changed_files": candidate.get("files") or [],
                    "changed_files_total": candidate.get("files_total") or 0,
                }
                if candidate.get("shared_with"):
                    entry["also_carries"] = candidate["shared_with"]
                if candidate.get("pull_request"):
                    entry["pull_request"] = candidate["pull_request"]
                if not selected and not candidate["in_default"] and label in repo_roots:
                    entry["diff"], entry["diff_truncated"] = _diff_sample(repo_roots[label], finding["base"], candidate["tip"])
                candidates.append(entry)
            if source == "story" and finding.get("default_commits"):
                merged_commits[display] = list(finding["default_commits"])
    return {"found": bool(candidates or merged_commits), "candidates": candidates, "merged_commits": merged_commits}


def _diff_sample(repo_path: str, base: str, tip: str) -> tuple[str, bool]:
    diff = run_git(repo_path, "diff", f"{base}...{tip}", check=False).stdout
    return diff[:_DIFF_SAMPLE_CHARS], len(diff) > _DIFF_SAMPLE_CHARS
