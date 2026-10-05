"""What has already been done on a ticket, found before forge creates a branch.

A team rarely hands forge an epic nobody has touched. A story is "In
Progress" because someone already has a branch for it —
`feature/PROJ-848-search-api`, ten commits ahead of main — and
forge used to ignore it: it branched `feat/PROJ-848-…` from main, and the
planner, reading main, planned the whole ticket again. The epic's pull
request then conflicted with the work it had duplicated.

So before an epic's first branch exists, the daemon asks git, for every
ticket key and every repo on the project: which branches carry this key,
how far ahead of the default branch are they, and is their work already in
it? No model is involved; everything here is a git query, and the answer
decides only WHERE forge's own branches start:

- `continue`: one branch has work the default branch lacks (or one contains
  every other candidate). Forge's story branch starts from it, so the
  planner reads that code and plans only what is missing.
- `choose`: several branches have diverged work and none contains the
  others. Forge does not guess which code to build on; the story waits for
  a human to pick one (or to start fresh). Nothing else waits on it except
  the stories that depend on it.
- `in_default`: the only branches found are already merged (squash merges
  included). Nothing to start from — the default branch has it — but the
  planner is told, so a ticket that is really done finishes as "already
  satisfied" instead of being rebuilt.
- `none`: nothing found.

The branch that is found is only ever READ: forge creates its own branch
from that commit and never writes to, pushes, or checks out the original.

Only a key that can appear in a branch name is looked for: a JIRA key like
`PROJ-848`. A GitHub issue's forge key (`gh.owner.repo.3`) has no such
form, so those stories are recorded as not applicable rather than matched
against every branch containing a `3`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from forge.tools.worktree import _fetch_origin, _repo_lock, default_branch, resolve_base_ref, run_git

# The most a finding records per branch — enough for a planner to know what
# the work was, bounded so an epic of long-lived branches stays a small row.
MAX_COMMITS = 15
MAX_FILES = 40
MAX_DEFAULT_COMMITS = 10

# `ENG-12`, `PROJ-848`: the JIRA key shape. Anything else (a GitHub story's
# `gh.owner.repo.3`) is not looked for — see the module docstring.
_JIRA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")


def is_searchable_key(key: str) -> bool:
    return bool(_JIRA_KEY_RE.match((key or "").strip()))


def key_pattern(key: str) -> re.Pattern[str]:
    """`key` as a whole token, any case: `PROJ-848` matches
    `feature/PROJ-848-search` and `proj-848`, never `PROJ-8480` or
    `XPROJ-848`."""
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(key.strip())}(?![0-9])", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def survey_repo(
    repo_path: str,
    keys: dict[str, list[str]],
    *,
    exclude_branches: set[str] | frozenset[str] = frozenset(),
    fetch: bool = True,
) -> dict[str, dict]:
    """{target: finding} for one repo.

    `keys` maps each target (a story or epic key) to every key that counts
    as it — the story's own plus its sub-tasks', since a branch may be named
    after either. `exclude_branches` are forge's own names (a story branch
    or epic branch forge already made), which are not prior work.

    Fetches first (best effort, under the repo lock) so a teammate's pushed
    branch is visible; `fetch=False` is for callers that just did."""
    if fetch:
        with _repo_lock(repo_path):
            _fetch_origin(repo_path)

    base = resolve_base_ref(repo_path, None)
    default = default_branch(repo_path)
    patterns = {target: [key_pattern(k) for k in aliases if is_searchable_key(k)] for target, aliases in keys.items()}
    branches = _branches(repo_path, exclude_branches)
    default_commits = _default_branch_mentions(repo_path, base, keys)

    matches: dict[str, list[dict]] = {target: [] for target in keys}
    owners: dict[str, list[str]] = {}
    for branch in branches:
        for target, target_patterns in patterns.items():
            if any(p.search(branch["name"]) for p in target_patterns):
                matches[target].append(branch)
                owners.setdefault(branch["name"], []).append(target)

    findings: dict[str, dict] = {}
    details: dict[str, dict] = {}  # one git walk per branch, however many targets it matches
    for target in keys:
        candidates = []
        for branch in matches[target]:
            if branch["name"] not in details:
                details[branch["name"]] = _describe(repo_path, base, branch)
            candidate = dict(details[branch["name"]])
            others = [t for t in owners.get(branch["name"], []) if t != target]
            if others:
                candidate["shared_with"] = others
            candidates.append(candidate)
        findings[target] = _verdict(repo_path, default, base, candidates, default_commits.get(target, []))
    return findings


def _branches(repo_path: str, exclude: set[str] | frozenset[str]) -> list[dict]:
    """Every local and origin branch, a local and origin copy of one name
    folded into one entry whose tip is the copy that contains the other.
    Copies that have diverged stay two entries — two different bodies of
    work, which is exactly the ambiguity a human has to settle."""
    proc = run_git(
        repo_path, "for-each-ref", "--format=%(refname)%09%(objectname)", "refs/heads", "refs/remotes/origin", check=False,
    )
    local: dict[str, str] = {}
    remote: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        ref, _, sha = line.partition("\t")
        if ref.startswith("refs/heads/"):
            local[ref[len("refs/heads/"):]] = sha
        elif ref.startswith("refs/remotes/origin/") and ref != "refs/remotes/origin/HEAD":
            remote[ref[len("refs/remotes/origin/"):]] = sha

    out: list[dict] = []
    for name in sorted(set(local) | set(remote)):
        if name in exclude:
            continue
        mine, theirs = local.get(name), remote.get(name)
        if mine and theirs and mine != theirs:
            if _is_ancestor(repo_path, theirs, mine):
                out.append({"name": name, "ref": name, "tip": mine, "unpushed": True})
            elif _is_ancestor(repo_path, mine, theirs):
                out.append({"name": name, "ref": f"origin/{name}", "tip": theirs})
            else:
                out.append({"name": name, "ref": name, "tip": mine, "diverged_from_origin": True})
                out.append({"name": f"origin/{name}", "ref": f"origin/{name}", "tip": theirs, "diverged_from_origin": True})
        elif mine:
            out.append({"name": name, "ref": name, "tip": mine})
        else:
            out.append({"name": name, "ref": f"origin/{name}", "tip": theirs})
    return out


def _is_ancestor(repo_path: str, older: str, newer: str) -> bool:
    return run_git(repo_path, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _describe(repo_path: str, base: str, branch: dict) -> dict:
    """How far `branch` is from the default branch and what it holds."""
    tip = branch["tip"]
    ahead = _count(repo_path, f"{base}..{tip}")
    behind = _count(repo_path, f"{tip}..{base}")
    log = run_git(
        repo_path, "log", f"-n{MAX_COMMITS}", "--format=%h%x09%an%x09%cI%x09%s", f"{base}..{tip}", check=False,
    ).stdout.splitlines()
    commits = [line.split("\t", 3) for line in log if line.count("\t") >= 3]
    last = run_git(repo_path, "log", "-1", "--format=%an%x09%cI", tip, check=False).stdout.strip().split("\t")
    files = run_git(repo_path, "diff", "--name-status", f"{base}...{tip}", check=False).stdout.splitlines()
    candidate = {
        "branch": branch["name"],
        "ref": branch["ref"],
        "tip": tip,
        "ahead": ahead,
        "behind": behind,
        "in_default": ahead == 0 or _already_in(repo_path, base, tip),
        "commits": [f"{c[0]} {c[3]}" for c in commits],
        "files": [" ".join(f.split("\t")) for f in files[:MAX_FILES]],
        "files_total": len(files),
        "last_author": last[0] if last and last[0] else None,
        "last_commit_at": last[1] if len(last) > 1 else None,
    }
    for flag in ("unpushed", "diverged_from_origin"):
        if branch.get(flag):
            candidate[flag] = True
    return candidate


def _count(repo_path: str, revision_range: str) -> int:
    out = run_git(repo_path, "rev-list", "--count", revision_range, check=False).stdout.strip()
    return int(out) if out.isdigit() else 0


def _already_in(repo_path: str, base: str, tip: str) -> bool:
    """True when merging `tip` into the default branch would change nothing.

    Commit ancestry cannot answer this for a squash- or rebase-merged branch:
    its commits are not the default branch's, but its changes are. Merging
    the trees can — if the merged tree is the default branch's own tree,
    every change on the branch is already there. A conflict means it is not."""
    merged = run_git(repo_path, "merge-tree", "--write-tree", base, tip, check=False)
    if merged.returncode != 0:
        return False
    merged_tree = merged.stdout.split("\n", 1)[0].strip()
    base_tree = run_git(repo_path, "rev-parse", f"{base}^{{tree}}", check=False).stdout.strip()
    return bool(merged_tree) and merged_tree == base_tree


def _default_branch_mentions(repo_path: str, base: str, keys: dict[str, list[str]]) -> dict[str, list[str]]:
    """{target: ["<sha> <subject>", ...]} — commits on the default branch whose
    message names one of the target's keys: work that was merged, possibly
    from a branch that has since been deleted. One `git log` for every key;
    git's regex dialect has no lookbehind, so it narrows by plain substring
    and the whole-token match is applied here."""
    searchable = {target: [k for k in aliases if is_searchable_key(k)] for target, aliases in keys.items()}
    plain = sorted({k for aliases in searchable.values() for k in aliases})
    if not plain:
        return {}
    out = run_git(
        repo_path, "log", base, "-i", "-F", *[f"--grep={k}" for k in plain], "-n2000", "--format=%h%x09%B%x1e",
        check=False,
    ).stdout
    patterns = {target: [key_pattern(k) for k in aliases] for target, aliases in searchable.items()}
    found: dict[str, list[str]] = {}
    for record in out.split("\x1e"):
        sha, _, message = record.strip().partition("\t")
        if not sha:
            continue
        subject = message.strip().splitlines()[0] if message.strip() else ""
        for target, target_patterns in patterns.items():
            if any(p.search(message) for p in target_patterns):
                bucket = found.setdefault(target, [])
                if len(bucket) < MAX_DEFAULT_COMMITS:
                    bucket.append(f"{sha} {subject}")
    return found


def _verdict(repo_path: str, default: str, base: str, candidates: list[dict], default_commits: list[str]) -> dict:
    unmerged = [c for c in candidates if not c["in_default"]]
    chosen: dict | None = None
    if len(unmerged) == 1:
        chosen = unmerged[0]
    elif unmerged:
        # One branch that contains every other one (a rework branched off
        # the first, say) holds all of it — no choice to make.
        for candidate in unmerged:
            if all(o is candidate or _is_ancestor(repo_path, o["tip"], candidate["tip"]) for o in unmerged):
                chosen = candidate
                break
    if chosen is not None:
        verdict = "continue"
    elif unmerged:
        verdict = "choose"
    elif candidates or default_commits:
        verdict = "in_default"
    else:
        verdict = "none"
    # Unmerged work first, most recent first: the order a human would scan.
    candidates.sort(key=lambda c: (c["in_default"], _reverse_time(c.get("last_commit_at"))))
    return {
        "verdict": verdict,
        "default_branch": default,
        "base": base,
        "chosen": chosen["branch"] if chosen else None,
        "candidates": candidates,
        "default_commits": default_commits,
    }


def _reverse_time(stamp: str | None) -> float:
    try:
        return -datetime.fromisoformat(stamp).timestamp() if stamp else 0.0
    except ValueError:
        return 0.0


# -- reading a stored lookup ---------------------------------------------
#
# The daemon stores one lookup per story (and one per epic):
#   {"looked_at": iso, "applicable": bool, "keys": [...],
#    "repos": {label: finding}, "picks": {label: branch or ""},
#    "errors": {label: message}}
# `repos` keeps only repos where something was found. `picks` is a human's
# answer to a `choose` ("" = start fresh). "Look again" leaves
# {"stale": True, "picks": {...}}: to be looked up afresh, with the picks
# kept where they still apply. These helpers are the only readers of that
# shape outside the daemon.


def lookup_due(prior_work: dict | None) -> bool:
    """Never looked up, or asked to look again."""
    return prior_work is None or bool(prior_work.get("stale"))


def carry_picks(previous: dict | None, repos: dict[str, dict]) -> dict[str, str]:
    """The picks from an earlier lookup that still answer a `choose` in the
    new one: same repo, and the branch picked is still among those found
    (or the pick was to start fresh)."""
    kept: dict[str, str] = {}
    for label, pick in ((previous or {}).get("picks") or {}).items():
        finding = repos.get(label)
        if finding and finding.get("verdict") == "choose" and (
            not pick or pick in {c["branch"] for c in finding.get("candidates") or []}
        ):
            kept[label] = pick
    return kept


# A ticket closed as one of these was dropped, not finished: whatever its
# branch holds is not work the epic builds on. Status names are all the
# source gives forge (forge/services/external_sync.py), so this reads them.
_DROPPED_STATUS_RE = re.compile(
    r"remov|won'?t|wont|cancel|reject|duplicat|invalid|obsolete|declin|abandon|not needed|deleted|gone",
    re.IGNORECASE,
)


def closed_as_done(status_text: str | None) -> bool:
    """Whether a ticket closed in its source was finished (Done, Closed,
    Resolved, …) rather than dropped (Removed, Won't do, Cancelled, …)."""
    return bool((status_text or "").strip()) and not _DROPPED_STATUS_RE.search(status_text or "")


def needs_choice(prior_work: dict | None) -> list[str]:
    """Repo labels where a human still has to pick a branch."""
    if not prior_work:
        return []
    picks = prior_work.get("picks") or {}
    return sorted(
        label for label, finding in (prior_work.get("repos") or {}).items()
        if finding.get("verdict") == "choose" and label not in picks
    )


def start_candidate(prior_work: dict | None, label: str) -> dict | None:
    """The branch forge's own branch in `label` starts from, or None for the
    usual start (the epic branch)."""
    if not prior_work:
        return None
    finding = (prior_work.get("repos") or {}).get(label)
    if not finding:
        return None
    picks = prior_work.get("picks") or {}
    wanted = picks[label] if label in picks else finding.get("chosen")
    if not wanted:
        return None
    return next((c for c in finding.get("candidates") or [] if c["branch"] == wanted), None)
