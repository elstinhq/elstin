"""Pulls a JIRA epic and its descendant tickets into elstin's local
bookkeeping (Epic/Story rows) so the daemon has something to poll.

Re-running against an already-synced epic picks up newly added child
tickets and refreshes computed levels; existing stories' status/progress
are left untouched — `StateManager.create_story` no-ops when the row
already exists. Summary/description on existing rows are also left alone
on a tree re-sync; use `sync_story_from_jira` /
`sync_epic_fields_from_jira` for an explicit prose refresh.

**The hierarchy is preserved, not flattened.** `JiraService.get_ticket_tree`
returns a breadth-first *flat* list of every descendant, which is not the
same thing as a list of the epic's children. Treating it as one turned
PROJ-3480's 6 stories + 25 sub-tasks into 31 independently-dispatched
stories: each story planned once as itself and again as each of its
sub-tasks, and sub-tasks that build on each other's code (PROJ-3487 extends
a model PROJ-3486 creates) sent to concurrent worktrees off the same base
where neither can see the other.

So the ticket set is split by depth:

- **Top-level stories** (parent is the epic, or no parent) are the unit of
  work: one worktree, one planner run, one PR each. Only these appear in
  `Epic.levels` and only these are ever dispatched.
- **Sub-tasks** are stored with `parent_key` set and status "skipped".
  They carry the file-level implementation detail that makes them
  excellent planner input, which the daemon folds into their parent's
  brief (see elstin/daemon/main.py's `_build_story_brief`) — but they are
  never dispatched in their own right. JIRA agrees: it marks them
  `hierarchyLevel: -1, subtask: true`, i.e. not independently deliverable.

**Story-level sync** is supported too. "Add ticket" accepts either a JIRA
Epic or a single Story/Task/Bug/…:

- **Epic** with children → children are the work units (above).
- **Epic** with no children yet → empty levels; wait for stories.
- **Non-epic** with no children → the ticket itself is the sole work unit
  (same key as the elstin epic row; `thread_id` is still the JIRA key).
- **Non-epic** whose only descendants are sub-tasks → same, with those
  sub-tasks as context under the root (not independently dispatched).

Levels here are a plain topological sort over JIRA issue links within the
synced set ("blocks" / "is blocked by") — every ticket with no such link
lands in a single wave 0. This is deliberately mechanical, not LLM-driven
epic planning (see elstin/db/models/gameplan.py's Epic.levels comment) —
just enough structure to unblock the reconciler.

QA tickets are the one exception to that mechanical sort: they are pulled
out of the topology entirely and appended as a single final level. A
validation ticket ("validate the implemented end-to-end flow") depends on
every implementation ticket in the epic by its nature, and teams almost
never express that with N explicit "is blocked by" links — left in the
topo sort, such a ticket lands in wave 0 and would run before the code it
is supposed to validate even exists. Forcing it last is not a heuristic
about ordering; it is what a validation ticket means.
"""
from __future__ import annotations

import logging

from elstin.services.external_sync import ReconcileReport, reconcile_epic
from elstin.services.jira_service import JiraTicket, issue_key_sort_key
from elstin.services.ticket_source import content_hash, get_ticket_source
from elstin.services.story_kind import KIND_QA, classify_story_kind
from elstin.state.manager import StateManager

logger = logging.getLogger("elstin.epic_sync")


def compute_dependencies(children: list[JiraTicket], kinds: dict[str, str] | None = None) -> dict[str, list[str]]:
    """{story key: the keys it must wait for}, for every top-level ticket.

    This — not `Epic.levels` — is what the reconciler schedules on: a story
    starts as soon as the stories it depends on have merged, whether or not
    anything else in the epic is still running. Dev stories depend on what
    their "blocks" / "is blocked by" links say (links leaving the synced set,
    or pointing at a QA ticket, are ignored); every QA ticket depends on
    every dev ticket, which is what a validation ticket means (see this
    module's docstring). A dependency cycle is broken deterministically — see
    `_break_cycles`."""
    kinds = kinds or {}
    dev_keys = {t.key for t in children if kinds.get(t.key) != KIND_QA}
    qa_keys = [t.key for t in children if kinds.get(t.key) == KIND_QA]

    depends_on: dict[str, set[str]] = {key: set() for key in dev_keys}
    for ticket in children:
        if ticket.key not in dev_keys:
            continue
        for link in ticket.linked_issues:
            other = link.get("key")
            if other not in dev_keys or other == ticket.key:
                continue  # outside the synced set, or a QA ticket — ignore
            link_type = (link.get("link_type") or "").strip().lower()
            if link_type == "is blocked by":
                depends_on[ticket.key].add(other)
            elif link_type == "blocks":
                depends_on[other].add(ticket.key)

    _break_cycles(depends_on)
    dependencies = {key: sorted(deps, key=issue_key_sort_key) for key, deps in depends_on.items()}
    for key in qa_keys:
        dependencies[key] = sorted(dev_keys, key=issue_key_sort_key)
    return dependencies


def _reachable(start: str, depends_on: dict[str, set[str]]) -> set[str]:
    """Every key `start` waits on, directly or through another story."""
    seen: set[str] = set()
    stack = list(depends_on.get(start, ()))
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        stack.extend(depends_on.get(key, ()))
    return seen


def _break_cycles(depends_on: dict[str, set[str]]) -> None:
    """Make the graph acyclic, in place and deterministically: the members
    of each cycle run one after another in issue-key order, and every
    dependency into or out of the cycle is kept. (The old level sort dumped
    whatever it could not place into one final level, together with
    everything waiting on the cycle.) Epics are a few dozen stories at
    most, so the quadratic reachability pass is not a concern."""
    reach = {key: _reachable(key, depends_on) for key in depends_on}
    handled: set[str] = set()
    for key in sorted(depends_on, key=issue_key_sort_key):
        if key in handled or key not in reach[key]:
            continue
        cycle = sorted((k for k in depends_on if k in reach[key] and key in reach[k]), key=issue_key_sort_key)
        members = set(cycle)
        for previous, member in zip([None, *cycle], cycle):
            depends_on[member] = {d for d in depends_on[member] if d not in members}
            if previous is not None:
                depends_on[member].add(previous)
        handled.update(members)


def levels_from_dependencies(dependencies: dict[str, list[str]]) -> list[list[str]]:
    """Group stories into levels (a story's level is one past its deepest
    dependency) — for display: the dashboard groups by level, and
    `Story.wave` is a story's level. Scheduling does not use levels.

    Ties within a level break on issue key, not on the source's result
    order, which is arbitrary — JIRA was returning PROJ-3480's stories
    newest-first, so the epic rendered 3485, 3484, 3483, …"""
    levels: list[list[str]] = []
    placed: set[str] = set()
    remaining = set(dependencies)
    while remaining:
        ready = sorted(
            (k for k in remaining if {d for d in dependencies[k] if d in dependencies} <= placed),
            key=issue_key_sort_key,
        )
        if not ready:
            # Unreachable for graphs from compute_dependencies (acyclic by
            # construction); a guard so a hand-edited row can never loop.
            ready = sorted(remaining, key=issue_key_sort_key)
        levels.append(ready)
        placed.update(ready)
        remaining.difference_update(ready)
    return levels


def _compute_levels(children: list[JiraTicket], kinds: dict[str, str] | None = None) -> list[list[str]]:
    """The levels `compute_dependencies` implies: dev tickets in link order,
    then every QA ticket in one final level — they depend on all the dev
    work, not on each other."""
    return levels_from_dependencies(compute_dependencies(children, kinds))


def relevel_epic(epic_key: str) -> list[list[str]]:
    """Recomputes an epic's dependencies and levels from the stories already
    in the DB, after a human has reclassified one (PUT /api/stories/{key}/kind).

    Works purely from stored state: each dev story keeps its stored
    dependencies on other dev stories, and a QA story depends on every dev
    story. So a story reclassified dev->qa stops being waited on by dev work
    and now waits on all of it; one reclassified qa->dev keeps the "every
    dev story" dependencies it had as QA — it lands after all the other dev
    work, which is always dependency-safe, since its real position in the
    link topology is not recoverable from here.
    """
    sm = StateManager()
    epic = sm.get_epic(epic_key)
    if epic is None:
        raise ValueError(f"No such epic: {epic_key}")

    # Sub-tasks are excluded outright: they are never dispatched, so they
    # never belong in levels. Their wave is mirrored from their parent
    # below, purely so the UI can sort them alongside it.
    stories = sm.list_stories_for_epic(epic_key)
    top_level = {s["jira_key"]: s for s in stories if s.get("parent_key") is None}
    kinds = {key: (s.get("kind") or "dev") for key, s in top_level.items()}
    subtasks_by_parent: dict[str, list[str]] = {}
    for story in stories:
        if story.get("parent_key"):
            subtasks_by_parent.setdefault(story["parent_key"], []).append(story["jira_key"])

    stored = stored_dependencies(epic["levels"] or [], top_level)
    dev_keys = {key for key, kind in kinds.items() if kind != KIND_QA}
    depends_on = {key: {d for d in stored.get(key, []) if d in dev_keys and d != key} for key in dev_keys}
    _break_cycles(depends_on)
    dependencies = {key: sorted(deps, key=issue_key_sort_key) for key, deps in depends_on.items()}
    for key, kind in kinds.items():
        if kind == KIND_QA:
            dependencies[key] = sorted(dev_keys, key=issue_key_sort_key)
    levels = levels_from_dependencies(dependencies)

    _store_levels_and_dependencies(sm, epic_key, levels, dependencies, subtasks_by_parent)
    logger.info("epic %s releveled: %s", epic_key, levels)
    return levels


def stored_dependencies(levels: list[list[str]], stories: dict[str, dict]) -> dict[str, list[str]]:
    """Each story's stored `depends_on`, or — for a row synced before
    dependencies were stored (NULL) — every story in an earlier level, which
    is exactly the order the old level-by-level scheduling enforced."""
    earlier: dict[str, list[str]] = {}
    seen: list[str] = []
    for level in levels:
        for key in level:
            earlier[key] = list(seen)
        seen.extend(level)
    result: dict[str, list[str]] = {}
    for key, story in stories.items():
        deps = story.get("depends_on")
        result[key] = list(deps) if deps is not None else earlier.get(key, [])
    return result


def _store_levels_and_dependencies(
    sm: StateManager,
    epic_key: str,
    levels: list[list[str]],
    dependencies: dict[str, list[str]],
    subtasks_by_parent: dict[str, list[str]],
) -> None:
    sm.set_epic_levels(epic_key, levels)
    for idx, level in enumerate(levels):
        for key in level:
            sm.set_story_wave(key, idx)
            sm.set_story_dependencies(key, dependencies.get(key, []))
            for subtask_key in subtasks_by_parent.get(key, []):
                sm.set_story_wave(subtask_key, idx)


def sync_epic_from_jira(jira_key: str, *, project_id: int) -> ReconcileReport:
    """Registers the epic and its child tickets under `project_id`, using
    that project's JIRA connection (its own override, or the global
    default — see elstin.config.store.resolve_config). A new epic is
    assigned the project's first-added repo as its default automatically
    (no repo is bound to the epic — every project repo is readable and
    writable by the plan, elstin/tools/workspace.py — this only fixes which
    one an unprefixed path means and gives dispatch a real path to create
    the first worktree from). A project with no repos yet leaves the epic
    folder-less until one is cloned via the Folders panel, at which point
    it gets backfilled the same way an in-progress re-sync backfills any
    older epic that predates its project's first repo.

    Raises ValueError if the epic already exists under a *different*
    project — re-syncing never silently reassigns an epic's project.

    Walking an epic that is already on the board also reconciles it with the
    source — content changes and closure — per elstin/services/external_sync.py;
    the returned report says what that did."""
    sm = StateManager()
    existing = sm.get_epic(jira_key)
    if existing is not None and existing["project_id"] != project_id:
        raise ValueError(f"{jira_key} already belongs to a different project (project_id={existing['project_id']})")

    # The project's first-added writable git repo, used below as the new
    # default-repo assignment (never a reference folder or a read-only repo:
    # an unprefixed path is written to the default). None when the project
    # has none yet — the epic then stays folder-less until one is added.
    default_repo_id = sm.writable_project_repo_id(project_id)

    source = get_ticket_source(project_id)
    jira_key = source.normalize_key(jira_key)
    tickets = source.get_ticket_tree(jira_key)
    root = next(t for t in tickets if t.key == jira_key)
    descendants = [t for t in tickets if t.key != jira_key]

    top_level, subtasks = _resolve_work_units(root, descendants)
    self_contained = len(top_level) == 1 and top_level[0].key == jira_key
    logger.info(
        "epic %s (%s): %d top-level stories, %d sub-tasks%s",
        jira_key,
        root.issue_type,
        len(top_level),
        len(subtasks),
        " (root is the sole work unit)" if self_contained else " (sub-tasks are context, never dispatched)",
    )

    classified = {
        ticket.key: classify_story_kind(
            summary=ticket.summary,
            description=ticket.description,
            issue_type=ticket.issue_type,
            labels=ticket.labels,
        )
        for ticket in top_level
    }
    # Levels are derived from kinds, so a re-sync has to read the *stored*
    # kind for any story that already exists rather than the freshly
    # classified one. `Story.kind` is never recomputed on re-sync (a human's
    # PUT /api/stories/{key}/kind override lives there) — but that guarantee
    # is worth nothing if the waves built on top of it are rebuilt from
    # prose heuristics as though the override never happened, which is
    # exactly what a story reclassified dev->qa would hit: the column stays
    # "qa" while the levels put it back in a dev wave.
    kinds: dict[str, str] = {}
    for key, (kind, reason) in sorted(classified.items()):
        existing = sm.get_story(key)
        if existing is not None:
            kinds[key] = existing["kind"]
            logger.info("story %s keeps stored kind=%s (re-sync)", key, existing["kind"])
        else:
            kinds[key] = kind
            logger.info("story %s classified kind=%s (%s)", key, kind, reason)

    dependencies = compute_dependencies(top_level, kinds)
    levels = levels_from_dependencies(dependencies)
    wave_of = {key: idx for idx, level in enumerate(levels) for key in level}

    sm.create_epic(
        jira_key, project_id=project_id, levels=levels,
        summary=root.summary, description=root.description,
        external_source=source.name, external_id=root.external_id or jira_key,
        project_repo_id=default_repo_id,
    )
    # create_epic only sets project_repo_id on insert — an older epic that
    # was first synced before this project had any repo (or before this
    # backfill existed) still needs it applied here, on re-sync, once a
    # repo shows up. Never overwrites a repo a human (or an earlier sync)
    # already assigned.
    current_epic = sm.get_epic(jira_key)
    if current_epic is not None and current_epic["project_repo_id"] is None and default_repo_id is not None:
        sm.set_epic_project_repo(jira_key, default_repo_id)
    sm.set_epic_levels(jira_key, levels)

    for ticket in top_level:
        kind, reason = classified[ticket.key]
        sm.create_story(
            ticket.key, epic_key=jira_key, wave=wave_of[ticket.key],
            summary=ticket.summary, description=ticket.description,
            kind=kind, kind_reason=reason,
            issue_type=ticket.issue_type, labels=ticket.labels,
            external_source=source.name, external_id=ticket.external_id or ticket.key,
            depends_on=dependencies.get(ticket.key, []),
        )
        # create_story no-ops on an existing row, but a "blocked by" link
        # added at the source since the last walk must still reach the
        # scheduler — dependencies (and the display wave) are recomputed on
        # every sync, like the levels above. Only a pending story's
        # dependencies are ever read, so rewriting a finished one's is inert.
        sm.set_story_dependencies(ticket.key, dependencies.get(ticket.key, []))
        sm.set_story_wave(ticket.key, wave_of[ticket.key])

    for ticket in subtasks:
        # Status "skipped" is belt-and-braces: sub-tasks are already absent
        # from `levels`, which is the only thing the reconciler iterates, so
        # they cannot be dispatched. The status makes that visible in the UI
        # and in any query that doesn't know about parent_key.
        # Kind is inherited from the parent rather than classified: a
        # sub-task is never routed on its own, so classifying it would
        # produce a value that means nothing and could contradict its
        # parent's.
        # Story-level sync: parent is the root itself (same key as the epic
        # row). Epic sync: parent is whichever top-level story owns them.
        parent_key = jira_key if self_contained else ticket.parent_key
        parent_kind, _parent_reason = classified.get(parent_key, ("dev", None))
        sm.create_story(
            ticket.key, epic_key=jira_key, wave=wave_of.get(parent_key, 0),
            summary=ticket.summary, description=ticket.description,
            kind=parent_kind, kind_reason="inherited_from_parent",
            parent_key=parent_key, status="skipped",
            external_source=source.name, external_id=ticket.external_id or ticket.key,
        )

    return reconcile_epic(
        sm, source, epic_key=jira_key, root=root, top_level=top_level, subtasks=subtasks, tickets=tickets,
    )


def sync_story_from_jira(story_key: str) -> dict:
    """Pulls the latest summary/description for one already-synced story
    from JIRA. Does not reclassify kind, relevel, or touch orchestration
    state — those stay under the human/daemon, not a prose refresh.

    Raises ValueError if the story (or its epic) is unknown, or if JIRA
    is not configured for the epic's project."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise ValueError(f"No such story: {story_key}")
    epic = sm.get_epic(story["epic_key"])
    if epic is None:
        raise ValueError(f"No such epic: {story['epic_key']}")

    ticket = get_ticket_source(epic["project_id"]).get_ticket(story_key)
    # A person asked for this refresh, so it is applied whatever the story's
    # state, and it resolves any change that was being held for them.
    sm.apply_external_content(
        "story", story_key, summary=ticket.summary, description=ticket.description or None,
        content_hash=content_hash(ticket.summary, ticket.description),
    )
    logger.info("story %s refreshed from JIRA", story_key)
    refreshed = sm.get_story(story_key)
    assert refreshed is not None
    return refreshed


def sync_epic_fields_from_jira(epic_key: str) -> dict:
    """Pulls the latest summary/description for one already-synced epic
    from JIRA. Levels, phase, folder, and child stories are left alone —
    use sync_epic_from_jira to pick up new child tickets."""
    sm = StateManager()
    epic = sm.get_epic(epic_key)
    if epic is None:
        raise ValueError(f"No such epic: {epic_key}")

    ticket = get_ticket_source(epic["project_id"]).get_ticket(epic_key)
    sm.apply_external_content(
        "epic", epic_key, summary=ticket.summary, description=ticket.description or None,
        content_hash=content_hash(ticket.summary, ticket.description),
    )
    logger.info("epic %s fields refreshed from JIRA", epic_key)
    refreshed = sm.get_epic(epic_key)
    assert refreshed is not None
    return refreshed


def _is_epic_issue_type(issue_type: str) -> bool:
    return (issue_type or "").strip().lower() == "epic"


def is_jira_subtask(ticket: JiraTicket) -> bool:
    """Public because filter-based ingestion needs the same judgement:
    elstin/services/jira_filter.py must drop sub-tasks a JQL query matched
    directly, for exactly the reason this module never dispatches them."""
    normalized = (ticket.issue_type or "").strip().lower().replace("-", "").replace(" ", "")
    return normalized == "subtask"


def _resolve_work_units(
    root: JiraTicket, descendants: list[JiraTicket]
) -> tuple[list[JiraTicket], list[JiraTicket]]:
    """Picks the dispatchable stories vs context-only sub-tasks for a sync.

    Epic roots keep the classic child-based split. Non-epic roots with no
    story-level children become the sole work unit so "Add ticket" works
    for a lone Story/Task.
    """
    if _is_epic_issue_type(root.issue_type):
        return _split_by_depth(root.key, descendants)

    if not descendants or all(is_jira_subtask(t) for t in descendants):
        return [root], list(descendants)

    # Non-epic root that still has story-level children — don't drop them.
    return _split_by_depth(root.key, descendants)


def _split_by_depth(epic_key: str, descendants: list[JiraTicket]) -> tuple[list[JiraTicket], list[JiraTicket]]:
    """Partitions the flat descendant list into (top-level stories,
    sub-tasks).

    A ticket is top-level when its parent is the epic itself, or when it
    reports no parent at all — the latter covers JIRA projects that link
    stories to an epic through the legacy "Epic Link" custom field rather
    than the `parent` field, where `parent_key` comes back empty.

    A ticket whose parent is neither the epic nor any top-level story
    (a sub-sub-task, or a sub-task whose parent fell outside the sync
    window) is treated as top-level, because the alternative is silently
    dropping it — an orphan that gets planned is recoverable, one that
    vanishes is not.
    """
    top_level_keys = {
        t.key for t in descendants if t.parent_key is None or t.parent_key == epic_key
    }
    top_level = [t for t in descendants if t.key in top_level_keys]
    subtasks = [t for t in descendants if t.key not in top_level_keys and t.parent_key in top_level_keys]

    orphans = [t for t in descendants if t.key not in top_level_keys and t.parent_key not in top_level_keys]
    if orphans:
        logger.warning(
            "epic %s: %s have a parent outside the synced top level — treating as top-level stories",
            epic_key, [t.key for t in orphans],
        )
        top_level.extend(orphans)
    return top_level, subtasks
