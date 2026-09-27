# AGENTS.md — Code Forge

Guidance for AI agents and humans working in this repository. Prefer this
file over inventing architecture: the module docstrings are normative, and
this document is the map of where those contracts live.

## What this project is

**code_forge** is a local, single-tenant JIRA-epic-to-PR platform. A
deterministic reconciler decides what to do next; a LangGraph loop runs
planner → dev → verifier (or epic QA) per story in an isolated git
worktree, with independent stories in parallel. Each finished story is
verified and merged into the epic branch **locally**; once every story has
merged, the epic opens **one** GitHub pull request, which a human reviews
and merges. The only per-story human gate is plan review (and the
planner's clarification questions).

Designed for a laptop: `docker compose up`, one process, Postgres-backed
state, no Kubernetes, no multi-tenancy.

## Quick start

```bash
scripts/forge.sh
# Dashboard: http://localhost:8000/
# Settings:  http://localhost:8000/settings
```

`scripts/forge.sh` is a wrapper over `docker compose` (or `podman
compose`): it rebuilds the image on every `up` so the container runs the
working tree rather than a stale copy of `forge/` and `alembic/`, waits
for `/healthz`, and adds `stop`/`down`/`logs`/`status`/`rebuild`/`psql`/
`reset`. `scripts/stop.sh` stops the stack (`--keep` to leave the
containers in place); only `reset` deletes data, and it asks first. Plain
`docker compose up` still works and nothing in `forge/` depends on either
script.

Local develop without the full stack:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up postgres -d
export DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
alembic upgrade head
pytest
```

**Only process env var:** `DATABASE_URL`. Everything else (JIRA, GitHub,
the LLM provider/key, gates, models) lives in Postgres and is edited live
at `/settings` — no `.env` file.

## Architecture at a glance

```
Settings UI ──HTTP──► FastAPI (forge/app.py)
                         │
                         ├── /api/*  (config, projects, epics, stories)
                         ├── lifespan → ticket-filter sweep (5m, per project)
                         └── lifespan → daemon poll loop (15s)
                                          │
                                          ├── StateManager (Postgres)
                                          ├── reconciler.plan_orchestration()  [pure]
                                          └── handle_intent
                                                ├── DispatchStory / ResumeStory
                                                │     → LangGraph (thread_id = JIRA key)
                                                │           ├── kind=dev: planner → [approval] → dev → tier-1 gate → verifier
                                                │           │                          ^_________|  (gate fail)  |
                                                │           │                          ^______________________|  (review fail)
                                                │           └── kind=qa:  epic QA → END
                                                ├── ParkForGate (plan approval)
                                                ├── MergeIntoEpic → merge queue: update from epic → gate → fast-forward
                                                └── (all merged) → epic PR: push epic branch, open one PR

Agent activity flows the other way, same process:

  roles/*  ──append_activity──►  activity_events (Postgres)
                                        │
                                        └── activity_stream.publish
                                              └── SSE /api/stories/{key}/stream
                                                    └──► Settings UI activity feed
```

JIRA is source of truth for tickets. Forge tables track orchestration
state only (status, kind, worktree, gates, levels).

## Package map (`forge/`)

| Package | Responsibility | Do not |
|---------|----------------|--------|
| `agents/` | Prompts + role runners (planner, dev, verifier, qa) + `loop.py`'s shared agent loop | Hand-roll a second chat loop, or call an `llm/` provider directly from a role — always go through `agents/loop.py` (dev/planner) or `services/generation.py` (verifier/qa) |
| `llm/` | The provider port: `types.py` (normalized vocabulary), `base.py` (`LlmProvider`/`Capabilities`), `openai_compat.py` (the OpenAI adapter — the only module that imports `openai`), `registry.py` (config → provider), `pricing.py`, `errors.py` | Import a provider SDK anywhere outside `llm/openai_compat.py` |
| `orchestration/` | LangGraph graph, pure reconciler, run state | Put I/O in the reconciler |
| `daemon/` | Poll loop, ticket-filter sweep, worktree create, intent handling, PR/CI/merge, QA brief assembly | Call agents without going through the graph |
| `state/` | **Only** DB access for epics/stories/PRs/projects | Import `forge.db.models` outside the allow-list in hard constraint #1 |
| `db/` | SQLAlchemy models + engine | Business logic here |
| `services/` | JIRA/GitHub clients, epic sync, ticket filters, story_kind, generation (verifier/QA's single-shot LLM call), repo clone / attach, run activity + its live fan-out, run telemetry (cost **and** run shape) | Bypass StateManager for persistence |
| `tools/` | Dev-agent tools, planner's read-only tools, language profiles, worktree, path guard, workspace (multi-repo read roots) | Hardcode language names — use `language_hooks`; give a reference repo a write path |
| `mcp/` | MCP servers (jira/github standalone) | Assume agents already use JIRA/GitHub MCP — daemon calls services directly today |
| `api/` | FastAPI routers | Long-running work — daemon owns that |
| `config/` | Postgres config store | Read secrets from env (except `DATABASE_URL`) |

Entry points (`pyproject.toml`): `forge-daemon`, `forge-mcp-jira`,
`forge-mcp-github`. Production serves via
`uvicorn forge.app:create_app --factory`.

## Multi-repo projects

**No repo is bound to an epic or a story.** A project may have several
repos (`ProjectRepo` is one-to-many); all of them are the workspace, the
planner reads all of them, and **the approved plan decides which ones get
written**. A story therefore writes one repo or several, discovered from
the task rather than configured ahead of it.

- `tools/workspace.py` owns the model: `Workspace(default_label, roots)`.
  A path with no prefix belongs to the *default* repo (the epic's assigned
  folder — a default, not a binding); `@label/path` names any other.
  `labels_in_plan()` is how the written set is derived from the plan.
- Labels come from `ProjectRepo.label` (else the directory name),
  slugified, colliding labels suffixed. An unknown label is an **error**,
  never a silent fallback — resolving it elsewhere would read the wrong
  file, or land an edit in the wrong repo.
- **Worktrees are per (story, repo)** (`story_worktrees`), created lazily
  by `dev_loop` for each repo the plan names, because that set is only
  known after planning. `Story.worktree_path`/`branch` mirror the default
  repo's row so the dashboard and single-repo paths are unchanged.
- **The gate runs per repo.** `run_deterministic_gate` partitions the plan
  by label and runs `compare_to_plan` in each worktree — a TypeScript UI
  and a Python API have different analyzers, and a planned API file does
  not exist in the UI worktree. Reports are merged with paths re-prefixed.
- **Commit and merge are per repo.** A story's merge verifies every repo
  it wrote before fast-forwarding any of them. The epic branch NAME stays
  per epic, giving a same-named branch in each repo — and its own
  integration worktree in each (`integration_worktree_path`; the first repo
  keeps `_integration-<epic>`), which is what keeps a cross-repo epic
  reviewable as one unit; epic QA diffs all of them. The epic opens one
  pull request per repo it wrote (`epic_pull_requests`).
- **Reads never go to a clone's own checkout** — it is frozen at clone time
  (an attached one is your live checkout) and knows nothing of the epic
  branch. `FeatureRunState.read_roots` (built by the daemon's `_read_roots`):
  a repo the story has a worktree in is read there, any other at the epic
  branch's worktree in that repo; the epic branch is made in **every**
  project repo at first dispatch for this. The planner, its explorers, the
  plan gate, the brief's codebase skeleton and dev's reads of repos it does
  not write all use it. `repo_roots` stays the clones — git operations and
  `_repo_lock` key on them. A repo the plan names later branches from the
  epic branch there (`story_base_refs`), not from its main.
- Plan scope still authorises every write. A writable repo is not a
  free-for-all: the path must be in the plan, prefix included.
- A single-repo project is byte-identical to before — no prefixes, no note
  in any tool description, and `make_dev_tools` returns the shared
  `TOOL_SCHEMAS` object rather than a patched copy.

**The trade-off, stated plainly:** a story spanning two repos produces two
branches, and git has no transaction across them, so it can land in one and
fail in the other. Forge makes that unlikely (every repo is verified before
any is fast-forwarded) and visible (a failed fast-forward fails the story's
merge) rather than pretending otherwise.

Migrations: `20260919_0018_story_project_repo.py`,
`20260919_0019_multi_repo_stories.py`.

### Where a project's repos come from: clone or attach

`services/repo_clone.py` gives a project a repo in one of two ways, both
behind `POST /api/projects/{id}/repos` (exactly one of `url` / `path`) and
project create (`repo_url` / `repo_path`):

- **Clone** a URL into `cloned_repos_base_dir/<project>/` — storage the
  app owns, so it works identically on the host and inside Docker.
- **Attach** a git repo already on disk, used in place
  (`attach_local_repository`). The path must be absolute, a directory the
  forge process can see, the repo's top level (a subfolder is refused,
  naming the top), have at least one commit, and not sit under
  `worktrees_base_dir`; one folder is attached once (`409`). Inside Docker
  that means a bind mount (`docker-compose.yml` has the commented example)
  and the in-container path; the image trusts every repo
  (`safe.directory '*'`) because a mounted one is owned by the host user.
- **Picking the path**: "Browse…" beside the path field walks the machine
  forge runs on (`POST /api/projects/browse-folder` → `browse_folder`:
  subfolder names only, hidden ones skipped, git repos marked). A browser
  cannot give a page a real path from the OS folder dialog, and the path
  that matters is forge's own — a mount point inside Docker.
- **A folder holding repos** (a workspace with one checkout per service,
  itself not a repo) is attached repo by repo: every git operation forge
  makes happens inside one repository. `scan_folder` /
  `POST /api/projects/scan-folder` lists the repos directly inside it (one
  level, hidden folders skipped, each with why it cannot be attached, if
  so); the UI asks which — none pre-selected, since every folder is read
  while planning — and attaches each as its own folder. Project create
  takes several at once (`repo_paths`).
- **Read-only repos** (`ProjectRepo.read_only`; the picker's per-repo box,
  or "Make read-only" on the Folders panel — `PUT .../repos/{id}`): read by
  every run, never changed. `Workspace.read_only` carries them: the tools
  tell the planner, the plan gate rejects a plan that creates or modifies a
  file there (`KIND_READ_ONLY_REPO`), and dev never gets a worktree in one.
  No epic branch is made there, no prior-work lookup runs there, and an
  epic never defaults to one (`writable_project_repo_id`); marking one
  moves epics that did, and waits (409) for unfinished work in it.
- **Reference folders** (`ProjectRepo.kind == "reference"`, always
  read-only): a plain folder read for context — the workspace folder the
  repos were picked from, with its CLAUDE.md about how they relate and its
  docs. Runs read a copy, never the folder (`tools/reference_folder.py`,
  refreshed at run start, reused for 60s): the repos inside it, hidden and
  dependency folders, data files (archives, dumps, spreadsheets, media) and
  anything over 1 MB are left out, up to 25 MB in all. The scan previews
  what would be read and left out; the picker offers it, ticked when there
  is anything to read. Its guide leads the project guide. A git repo is
  refused as a reference folder — attach it read-only instead. The copy
  lives under `worktrees_base_dir/_reference/<id>` and goes with the folder
  or the project.

**Removing a folder** (`DELETE /api/projects/{id}/repos/{repo_id}`) detaches
it; the folder on disk stays. An epic's folder is only a default, so epics
that defaulted to it move to the project's next folder (none if it was the
last — `poll_once` assigns one when a folder is added) and stories that
pointed at it inherit their epic's again. It is refused (409) only while a
story has unfinished work in it — a worktree there, not merged/done/skipped.

**An attached folder's own checkout is never touched** — forge only adds
branches and worktrees to its `.git`, exactly as with a clone. An attached
repo may have no `origin` (never pushed): `tools/worktree.py` then skips
the fetch and branches from the local default branch (`origin/<default>`,
else local `main`/`master`, else `HEAD`), stories merge locally as usual,
and the epic ends `complete` with the reason recorded on its
`epic_pull_requests` row ("origin is not a GitHub remote") — there is
nothing to open a PR against.

## Project guide (an AGENTS.md for the repos forge works on)

The planner and dev prompts have a slot for the target repo's own guide —
its layout, commands and what already exists — so the agents do not
rediscover it every run (or get it wrong: one run planned a new codegen
script for a repo that already had one). `services/project_guide.py` fills
it, per repo, in this order: the repo's own `AGENTS.md`, then `CLAUDE.md`,
then a guide forge generated and stored, then nothing.

- Resolved by the daemon at first dispatch (`project_guide_for_run`) and
  carried in `FeatureRunState.project_guide` → planner → `context_bundle`,
  which is how dev reads it too. Several repos are joined under a heading
  each; each guide is capped at `MAX_GUIDE_CHARS`.
- **Generation is opt-in**: the project-overridable `generate_project_guide`
  setting (off by default, it spends tokens) makes a run write a guide for
  any repo that has neither its own nor a stored one. The same is available
  on demand at `POST /api/projects/{id}/repos/{repo_id}/guide/generate`, and
  from the Folders panel in the UI.
- A reference folder's own CLAUDE.md/AGENTS.md comes first — it is the one
  that says how the repos relate (e.g. "this contract repo is off limits").
  forge never generates a guide for a reference folder.
- The generated guide lives on `ProjectRepo.generated_guide`. **forge never
  writes it into the repo** — it does not edit checkouts it does not own —
  and a repo that ships its own guide is never generated for, and cannot be
  overwritten through the API (`409`).
- It is one single-shot call, **on the planner's model** (`planner_sdk_model`,
  not the smaller verifier/QA one, which returned a title in place of the
  document), over gathered facts (a shallow listing,
  manifests, READMEs, CI workflows, a code skeleton), with a prompt that
  forbids claiming anything is *missing* — the listing is a partial view.
  A reply that is not a document (under 200 characters, or without section
  headings) is retried once with the reason and then refused, never stored.
  A generation failure is logged and skipped, never a run failure.
- The guide is read as authoritative, so a wrong line gets followed: the API
  and UI let a person read, edit (`PUT .../guide`), regenerate or discard it.

## Work that already exists for a ticket

Tickets rarely arrive untouched: a story is "In Progress" because someone
already has `feature/MKT2-848-customer-search-api`, ten commits ahead of main.
Before any of an epic's branches exist, forge looks (git only, no model —
`services/prior_work.py`; the daemon's half in `daemon/prior_work_lookup.py`):

- **What:** every local and `origin/` branch whose name carries the ticket's
  key as a whole token (a story's sub-task keys count for it), in every repo
  on the project, plus commits on the default branch naming it. Per branch:
  commits ahead/behind, files, last author, and whether its changes are
  **already in** the default branch (`git merge-tree --write-tree` yields the
  default branch's own tree — this catches squash merges). JIRA-style keys
  only; a GitHub issue's key has no form that appears in branch names.
- **Verdict per repo:** `continue` (one unmerged branch, or one containing
  all the others), `choose` (diverged, none contains the rest), `in_default`
  (only merged work), `none`. Stored on `stories.prior_work` /
  `epics.prior_work` (migration 0028).
- **When:** `poll_once`, for every active epic, before anything dispatches —
  in manual-run projects too, so findings show as soon as the epic is added.
  A story is looked up once, while pending with no branch; the epic's key
  until its branch exists. First dispatch looks up inline if still missing.
  `POST /api/epics/{key}/prior-work/refresh` ("Look again") clears it for
  stories not started yet.
- **What it changes** (first dispatch only, `_execute_story_run`): the
  story's branch starts from the `continue` branch (the ref, so commits
  pushed since come too), then the epic branch is merged in
  (`update_story_from_epic`; a conflict is aborted and left to the merge
  queue). Non-default repos with a `continue` get their worktree eagerly, so
  the planner reads that code. The epic branch starts from a branch named
  after the epic, or from forge's own epic branch already on origin
  (`ensure_integration_worktree`'s `start_point`). The found branch is only
  ever read: forge never writes to, pushes or checks it out.
- **`choose`:** forge does not guess which code to build on. The reconciler
  holds the story (`StorySnapshot.needs_branch_choice` →
  `choose_prior_branch`); dependents wait as for any dependency, nothing
  else does. `POST /api/stories/{key}/prior-work {repo_label, branch|null}`
  records the pick (`null` = start fresh); the UI's "Choose branch".
- **The planner** gets it in the prompt's prior-work block
  (`planner_prior_work` → `_format_prior_work_block`): SELECTED (on disk),
  ALREADY MERGED (on disk via main), or reference only (with a diff sample to
  port). Work already in main is not skipped — the planner confirms what is
  missing, and the already-satisfied path finishes a ticket that is done.
- **Landing:** a story whose branch carries continued commits merges even
  when dev changes nothing (see below); the epic PR body names each
  continued branch.
- **Tickets closed as done** are looked up too: "Done" in the source is not
  "in the default branch" — a team closes a ticket when its work is
  reviewed, and the branch may still wait to merge. Each done ticket's
  `continue` branch (or pick) is merged into the epic branch
  (`_bring_done_work_into_epic`, under the epic's merge lock; every poll
  and at first dispatch, before any story branches), so every story plans
  and builds on it. A ticket closed as dropped (`closed_as_done`: Removed,
  Won't do, Cancelled, deleted…) is not taken; a branch that conflicts is
  undone and flagged, never forced. The outcome is recorded per repo on
  the ticket's `prior_work["epic_merge"]` (retried only when its branch
  moves) and named in the epic PR body.
- **Plans made before done work arrived are made again**
  (`_replan_behind_done_work`): a story waiting for plan approval whose
  planning started before the latest such merge has its worktrees updated
  from the epic branch and goes back to `pending`
  (`send_back_to_planning`, questions and answers cleared).

## Cancelling a run

`POST /api/stories/{key}/cancel` (the **Cancel run** button on a running story)
stops a run in flight. Pause only stops the *next* dispatch, so before this the
only way to end a run that had gone wrong was to restart the whole stack.

- Each run executes in **its own asyncio task**, registered by story key in
  `daemon/main.py` (`_run_tasks`). That is what makes cancelling safe: the poll
  loop awaits runs inline, and cancelling *that* task would stop the daemon.
  `_run_or_resume_story` tells a person's cancel (`_cancel_requested`, and its
  own task not being cancelled) from a shutdown (the caller cancelled — still
  restores the story and re-raises, as before).
- A cancelled story becomes **`failed`, `parked_reason="cancelled"`,
  `failure_stage="run"`** and a "Run cancelled" activity event is written, so the
  existing Retry (a full redispatch) and Remove both apply. If the run had
  already written its outcome when the cancel landed, that outcome stands.
- The route is `async` on purpose: cancelling a task must happen on the event
  loop's thread. It answers `409` when no run is in flight in this process.
- **Limit:** a model call is abandoned at once, but a tool already running in a
  worker thread (a build, a test run) cannot be interrupted; it finishes and its
  result is discarded. Model calls already made are still billed.

## When a planned file already does the job

The gate requires every file the plan says to modify to have been modified, and
had no honest way to hear "this one needs nothing". One run whose ticket was
already implemented did what that forces: it made javadoc and `required`-marker
edits "so every planned file is actually written". Now it can say so.

- **Dev tool** `declare_already_satisfied_tool(path, reason)` (`tools/dev_tools.py`).
  Only a file the plan *modifies* qualifies (one to create must be written), it
  must exist, and the reason must be specific (`MIN_DECLARATION_REASON_CHARS`).
  It marks the checklist entry done. It is bookkeeping, not progress: it does not
  reset the idle budget or the repeat-call guard. The dev prompt offers it and
  forbids "a comment, rename or reformat just to make a planned file count".
- **Gate:** `compare_to_plan(declared_unchanged=…)` no longer blocks a declared,
  existing, unchanged file; it lists it as `assumed_satisfied` (per repo, paths
  re-prefixed on merge). A declaration never excuses a missing file, and one
  that was in fact changed is not reported as assumed. `format_gate_feedback`
  offers the tool when files "were not modified".
- **Reviewer:** the LLM review is *always* run when anything was declared — the
  trivial-diff skip would otherwise pass a run that changed nothing — and its
  prompt tells it to check each claim and not to fail a run for having few or
  no changes.
- **Ending:** a run that changed nothing, with declarations, completes as story
  status `done`, `parked_reason="already_satisfied"` (an activity event lists each
  file and reason) instead of trying to open an empty pull request — unless its
  branch carries commits the epic branch lacks (earlier work it continued, see
  "Work that already exists"): then it is `ready_to_merge`, so those land. A run with
  no declarations and no changes is unchanged. The state field is
  `FeatureRunState.declared_unchanged`, carried into fix rounds.

## Removing a ticket

`DELETE /api/epics/{key}` (the trash button on a ticket card) forgets a ticket
locally and completely — `daemon/ticket_removal.py`, with the row-level half in
`state/manager_removal.py`. A "ticket" is what Add ticket adds: an epic and its
stories and sub-tasks.

- **Deleted:** the epic/story rows and everything keyed by them (pull-request
  and worktree records, activity events, usage, run outcomes, watermarks); each
  story's worktree and *local* branch and the epic's integration worktree and
  branch, in every repo on the project (the epic branch is made in each); the story and integration folders
  under `worktrees_base_dir`, `data_dir/baselines/<key>` and
  `jira_attachments_dir/<key>`; and the LangGraph checkpoints for each story's
  thread. The checkpoints matter beyond tidiness: a ticket added again under
  the same key would otherwise resume the old thread.
- **Never touched — local only:** nothing is sent to GitHub or JIRA. An opened
  pull request stays open and a pushed branch stays pushed; the report's
  `left_on_remote` lists them so they can be closed by hand. forge keeps no
  record that a ticket was removed, so a project whose ticket filter still
  matches it ingests it again on the next sweep (`ticket_filter_may_readd`).
- **Order and guards:** a story with a run in flight is refused (`409`), and the
  ticket's keys are claimed in `daemon/main.py`'s live-run registry for the
  duration, so the poller and `/start` cannot begin a run mid-removal. The
  footprint is read first, then the filesystem, then checkpoints, then the rows
  *last* — a failure before that leaves the ticket listed and removable again
  instead of orphaned files. Filesystem problems are warnings (a worktree
  already deleted by hand is no reason to keep a ticket); a checkpoint failure
  stops the removal. Directories are only removed under their configured base,
  a symlink is unlinked rather than followed, and a repo's default branch is
  never deleted. `handle_intent` skips an intent for a story removed since the
  cycle's snapshot was taken.

## Deleting a project

`DELETE /api/projects/{id}` (`daemon/project_removal.py`; the Delete section
at the bottom of the project's Settings dialog, confirmed by typing the
project's name) deletes the project and everything forge holds for it —
locally, like removing a ticket:

- **Each ticket**, exactly as Remove ticket does (above): rows, worktrees,
  local branches, the epic branch in every repo, checkpoints, baselines,
  attachments.
- **The clones forge made** — a folder added by URL, under
  `cloned_repos_base_dir/<project id>` — and that folder once empty. A
  stored path outside it is reported, never deleted.
- **The project's rows**: settings overrides, ticket filter, folders, the
  project (`state/manager_project.py`'s `delete_project_records`, one
  transaction; it refuses while any epic still belongs to the project).
- **Never**: an attached folder (someone's own checkout — only the branches
  and worktrees forge added go, with the tickets), and anything on GitHub
  or JIRA. The report lists open pull requests and possibly-pushed branches.
- **Order**: a run in flight anywhere in the project refuses it (409) before
  anything is touched. Then the ticket filter is dropped and every epic put
  on hold, so no sweep or poll starts new work; tickets go one at a time;
  clones and rows last. A failure part-way leaves the project listed and
  deleting it again finishes.

## Story kinds and hierarchy

### `dev` vs `qa`

- Classified once at sync (`forge/services/story_kind.py`): labels >
  issue type > summary/description heuristics.
- Biased toward **`dev`** — inventing a QA story stalls less badly than
  missing implementation work.
- Persisted on `Story.kind` / `kind_reason`. **Never recomputed on
  re-sync** (`create_story` no-ops if the row exists).
- Human override: `PUT /api/stories/{key}/kind` (pending top-level
  stories only) → `relevel_epic()`.

### Parent / sub-tasks

- Only **top-level** stories are dispatched (worktree, run, PR).
- Sub-tasks get `parent_key`, status `skipped`, are omitted from
  `Epic.levels`, and enrich the parent brief in the daemon.
- **Story-level sync:** settings "Add ticket" accepts an Epic or a lone
  Story/Task. Non-epic roots with no story-level children become the sole
  work unit (same JIRA key as the epic row); empty Epics get no levels until
  children appear. See `forge/services/epic_sync.py` for the full contract.
- **Filter sync:** a project can instead carry one saved JIRA query
  (`ProjectTicketFilter`) that a slow background loop re-runs, ingesting
  every new root it matches. Structured fields compile to JQL in
  `forge/services/jira_filter.py`. A sweep ingests every new root, and
  **re-walks** roots forge already has (and every epic still in progress,
  whether or not the filter matches it any more — the filter selects on
  status, so a finished epic drops out of it) through the idempotent
  `sync_epic_from_jira`: `create_epic`/`create_story` no-op on existing
  rows, so a story mid-run is never restructured. The re-walk then
  reconciles content and closure (below). Sub-tasks, children of other
  matches, and keys owned by another project are skipped, and nothing is
  unsynced when a ticket stops matching. Ingested epics land paused and
  folder-less, exactly like "Add ticket".
- **The board vs. its source** (`forge/services/external_sync.py`,
  `ticket_source.py`): the Postgres board is what agents read; the source
  owns what a ticket *says*. A ticket whose text changed is applied at once
  when no plan depends on it (unplanned or finished story, sub-task, epic)
  and **held** on the story (`external_pending`) when one does — a human
  applies it, applies and re-plans, or dismisses it
  (`POST /api/stories/{key}/external-change`). A ticket the source reports
  closed or deleted stops being dispatched: an unstarted story becomes
  `skipped` (parked_reason `closed_externally`), an in-flight one is only
  flagged, a closed epic is put on hold. A story is only called deleted
  after the source confirms it, and never from a walk that hit its size cap.
  Orchestration state (kind, gates, worktrees, PRs, levels) is never touched.
- **Sources** are per project (`projects.ticket_source`, `PUT
  /api/projects/{id}`): `jira`, or `github` (`services/github_issues.py`,
  reads with the global `github_token`; comments/closing are off until
  `github_issues_write_enabled`). A GitHub issue's board key is
  `gh.<owner>.<repo>.<number>`, lower-cased — *not* `owner/repo#12`, whose
  `/` breaks `/api/stories/{key}`, branch names and worktree paths — and
  the readable `owner/repo#12` is its `external_id`. `normalize_key` turns
  what a person types (`owner/repo#12`, an issue URL) into the board key;
  keys over 64 characters (very long owner + repo) are refused. Hierarchy is
  GitHub sub-issues (root with children = epic, sub-issue of a story =
  sub-task), dependencies are "blocked by", closed is `state == closed`, kind
  comes from labels. A source's tickets keep their source if the project is
  later switched.
- Migrations: `alembic/versions/20260817_0007_story_kind.py`,
  `20260817_0008_story_parent.py`.

### Dependencies (and levels, for display)

`stories.depends_on` holds the stories each one waits for, from the
source's blocks / is-blocked-by links among **dev** stories
(`epic_sync.compute_dependencies`, rewritten on every sync so a link added
later still counts); every QA story depends on every dev story. A cycle is
broken deterministically: its members run one after another in key order.
**The reconciler schedules on these:** a story is ready as soon as each of
its dependencies is `merged` / `done` / `skipped` — independent stories run
in parallel, and a story waits only for its own blockers. A failure stops
only the stories that depend on it (directly or transitively); the epic is
`failed` only once nothing left can make progress. `Epic.levels` / `wave`
are derived from the same graph purely to group rows in the UI. A row
synced before `depends_on` existed falls back to "every story in an earlier
level" (`epic_sync.stored_dependencies`), which is the old level order.

The planner of each story also sees its epic: the epic's text and every
sibling story with its state (`_epic_context_for_brief` in
`daemon/main.py`), so it neither guesses the epic's vocabulary nor plans a
sibling's work, and builds on the ones already merged.

## Agent roles

| Role | Module | Runtime | Notes |
|------|--------|---------|-------|
| Planner | `planner.py` + `planner_loop.py` | `agents/loop.py` (tool-driven) | `tools/planner_tools.py`'s read-only tools only; structured JSON plan via the provider's native structured output |
| Explorer | `explorer.py` | `agents/loop.py`, N concurrent sub-loops | Read-only recon *inside* the planner step, not a graph node. Scope -> fan out -> fold findings into the plan prompt. Prompt adapted from Aster's `scout` agent (Apache-2.0) |
| Dev | `dev.py` + `dev_loop.py` | `agents/loop.py` (tool-driven) | **Only** tools from `tools/dev_tools.py`'s `make_dev_tools()` — handed to the loop directly, no MCP layer |
| Verifier | `verifier.py` | Tier 1 deterministic gate (`run_deterministic_gate`, run by the graph's own `gate` node before the verifier is ever entered) + `generation.chat_completion_json` review | Language-specific `compare_to_plan` via hooks; `format_gate_feedback` turns a gate failure into dev's next user message — plan-coverage blockers carry a `kind` (`KIND_*`) and become a "planned files still not written" section that names the tool per file and says to write now, with a `wrote_nothing` line (and last-attempt warning) when the worktree is untouched, rather than a "fix these failures" list; `_VERIFIER_JSON_SCHEMA` constrains the review |
| QA | `qa.py` (`run_epic_qa`) | `generation` + full test suite | Epic-level validation ticket; suite failure fails the run, subjective LLM verdict does not. `QA_JSON_SCHEMA` constrains the report, since `format_qa_report_comment` is its only consumer and reads specific keys |

**Planner exploration** (`agents/roles/explorer.py`): before the plan
loop, a scoping call picks up to `planner_explorer_max_agents` independent
investigation areas and one read-only sub-agent answers each, concurrently.
The point is context isolation, not speed: a file the planner reads itself
stays in its history and is re-sent every later turn, whereas a sub-agent's
reads die with its throwaway context and only a short report comes back.
Everything about it is best-effort — an empty scope, a failed scope, or
every explorer returning nothing all degrade to "plan without a head
start". Sub-agents get the read-only tools only, so there is no write path
and no way to spawn another one. Skipped on a clarification round-trip.
Their tokens roll up into the planner's `story_usage` row via the same
`on_turn` hook, and their spend caps are carved out of
`planner_sdk_max_budget_usd` rather than each inheriting the whole cap.

**Agent loop mechanics** (`forge/agents/loop.py`, model-agnostic — the
same loop drives GPT, DeepSeek, or a local vLLM/Ollama endpoint):

- Compaction is **tombstoning, not summarization**: tool results (and old
  copies of the trailing reminder) older than the last N turns are replaced
  in place with a fixed string; no message is removed or reordered, no LLM
  call is made, and the prompt prefix stays byte-stable for provider prefix
  caching.
- **Context overflow** (`LlmContextOverflowError` — the adapter maps the
  provider's "prompt is too long" 400, or a 413): the loop compacts deeper
  (only the latest turn kept, large string arguments of older tool calls —
  a written file's content — tombstoned too) and retries the same turn
  once. A second overflow in that turn, or nothing left to compact, ends
  the run as before.
- **Tool arguments are checked before a tool runs**
  (`agents/tool_arguments.py`): repaired where the tool's JSON schema makes
  the intent unambiguous (`"200"` → 200, `"false"` → False — Python reads
  the string `"false"` as true, which once made `write_file_tool`
  overwrite a file), then validated. A call still invalid, or whose
  arguments were not JSON at all (`ToolCallBlock.arguments_error`), gets
  an is_error result naming the problem and the tool does not run.
- Consecutive **read-only** tool calls (`tools/dev_tools.py` /
  `tools/planner_tools.py`'s `READ_ONLY_TOOLS`) run concurrently; every
  other tool runs alone, in order. A new tool is mutating until declared
  otherwise.
- **Repeat-call guard**: an identical (name, args) call in the consecutive
  turn is intercepted and answered with an is_error nudge; resets after any
  mutating call.
- **Model-call deadline** (`llm_request_timeout_seconds`, default 900): the
  adapter (`llm/openai_compat.py`) abandons a single call after this many
  seconds and raises `LlmTimeoutError`. The SDK's own timeout is per read, and
  a gateway that answers a slow request with keep-alive bytes never trips it —
  one run's single call sat for 2h39m. A timed-out call is retried **once**
  (`_MAX_TIMEOUT_RETRIES`; other transient errors keep their three tries),
  because a provider that stalled tends to stall again and each attempt costs
  the whole deadline. The roles' `on_retry` hook writes "Model call failed —
  retrying" to the activity feed so the gap is never silent. 0 turns it off.
- **Idle-turn budget** (dev only, `dev_sdk_max_idle_turns`): N consecutive
  tool turns with no `PROGRESS_TOOLS` call end the run as a soft stop
  (`no_progress`), same handling as a turn cap. The configured N is the
  base for a small plan: `idle_budget_for_plan` (`agents/roles/dev_loop.py`)
  adds one turn per two further planned files, capped at 4× the base — a
  15-file change cannot be understood in the turns that suit a one-file fix,
  and a run cut off mid-exploration wrote nothing three rounds running. The
  cap keeps it a bound on a model that is only looping. In the last
  `IDLE_WARNING_WINDOW` turns before the stop, the trailing message also
  says how many are left (`_idle_warning`), so the cut-off is never a surprise.
- **Trailing reminder** (`LoopConfig.trailing_reminder`): a callable
  re-evaluated each turn whose text is appended to the request as a final
  user message — and **kept in the loop's history**, so every request
  begins with every message the one before it sent. It used to be sent
  and dropped; GPT-5.6+ caches the prompt only at the end of the latest
  user/tool message, which was always that reminder, so nothing past the
  system prompt was ever reused (one dev run: 4% of 11.5M input tokens
  cached, against 59% for the planner on the same model and gateway).
  Compaction tombstones stale copies, and `LoopResult.history` — what is
  persisted and resumed — never contains any. Dev uses
  it to re-show the **progress checklist** (`tools/progress.py`): one
  entry per planned file, seeded from the plan, updated by the model via
  `update_progress_tool`, carried across fix rounds in
  `FeatureRunState.dev_progress`. It is what turns the idle-turn budget
  from a silent stop into a correction the model can act on. Ticking a box
  is **not** in `PROGRESS_TOOLS` — counting it would hand the model a way
  to reset the idle budget forever without touching a file.
- `read_file_tool` serves at most 250 lines per call (`tools/grep.py`'s
  `MAX_READ_LINES`); a larger `limit` is rejected, an unwindowed read of a
  longer file returns the first 250 with a continue-from offset.
- `outline_file_tool` (`tools/outline.py`, dev and planner) lists a file's
  classes, functions and methods with line numbers, so the next read can
  be one method's range. Language-neutral heuristics — nothing branches on
  a language name. It exists because one dev run spent 237 of 341 calls
  searching for `class X` and paging through files from line 1.
- `grep_codebase` caps matches **in total** (ripgrep's `--max-count` is per
  file — "15" used to mean one match per file across the repo), stops
  ripgrep once it has enough, sorts by path, previews lines longer than
  `MAX_MATCH_LINE_CHARS`, and passes the pattern with `-e` so one starting
  with `-` is not read as an option. Callers ask for one more than they
  show to know when to say the list was cut.

**Pointing forge at a non-OpenAI model** (any OpenAI-compatible endpoint
via `llm_base_url` — OpenRouter, vLLM, Ollama, Azure):

- `reasoning_effort` is gated per model by `llm/openai_compat.py`'s
  `_EFFORT_SUPPORT`, which records both *which* models take the field and
  *which levels* each one takes. Both mistakes are a 400, and a 400 is a
  non-retryable `LlmBadRequestError` that kills the run — so an
  unsupported level is dropped (with a warning) rather than forwarded, and
  an unlisted model never gets the field at all. Adding a model means one
  row, with its levels read off that model's own docs.
- `llm/pricing.py` needs a row per model you actually run. Every
  OpenAI-compatible endpoint resolves as provider `"openai"` (the registry
  names the adapter, not the base_url), so third-party model ids go in
  *that* dict. A missing row makes `estimate_cost_usd` return None, which
  silently disables `max_budget_usd` for that model — a test pins every
  config default against the table for this reason. A dated snapshot id
  (`gpt-5.6-luna-2026-08-01`, OpenRouter's `…-20260813`) matches its
  undated row. A row can carry a `cache_write_per_mtok` for models that
  bill cache writes above the input rate (GPT-5.6+).
- **OpenRouter reports what it billed** (`usage.cost`, on every response);
  the adapter reads it into `Usage.cost_usd`, and both the loop's budget
  and `story_usage` use it in preference to the table. The table can only
  guess which upstream served a call — its DeepSeek row overstated one
  run's spend 2.3×.
- Raise/lower `agent_compaction_token_threshold` to suit the context
  window; the default assumes roughly 1M tokens.

Every role reaches the LLM through `forge/llm/registry.py`'s configured
provider — never a provider SDK directly. `dev`/`planner`'s multi-turn,
tool-calling runs go through `forge/agents/loop.py`; `verifier`/`qa`'s
single-shot JSON completions go through `forge/services/generation.py`
instead, since a full agent loop would be pure overhead for a call with no
tools and no multi-turn state. Dev's turn-capped resume state
(`dev_history`, forge/llm/types.py's normalized message history,
JSON-serialized) is a plain `FeatureRunState` field — durably checkpointed
by LangGraph's `AsyncPostgresSaver` alongside every other piece of run
state, not a bespoke table.

**Graph paths** (`forge/orchestration/graph.py`):

- `dev` → `planner` → (optional interrupt) → `dev` → `gate` → `verifier` → `END`
  - `gate` is the Tier 1 fast-fail: compare_to_plan (planned files present,
    compile/lint, planned tests — subprocesses, no model). Fail → straight
    back to `dev` with a targeted fix message (`gate_feedback`, appended to
    the run's existing `dev_history`); three consecutive fails →
    `failed`. Pass → `verifier`, which reuses the gate report
    (`tier1_gate`) instead of re-running the toolchain. Review fail →
    `dev` (verifier iteration budget, separate from the gate's).
  - A run that ended `completed` can be **re-entered at `dev`** by the merge
    queue: `graph.aupdate_state(..., as_node="gate")` with
    `status="developing"`, a `gate_feedback` brief and `integration_paths`,
    then an ordinary resume continues dev → gate → verifier → END.
  - `verifier` and `qa` catch `services/generation.py`'s `GenerationError`
    (provider call failed, or the reply carried no JSON object) and return
    `status="failed"`, for the same reason `dev` catches `DevLoopError`:
    nothing upstream catches a per-story exception, so one unusable model
    reply used to abandon every epic queued behind it for the rest of the
    poll cycle. Neither role asks for a strict schema — both rely on a
    prompt plus `_parse_json_content`'s tolerant extraction — so this is
    the normal failure mode for a chatty model, not an exotic one.
- `qa` → `qa` → `END`

Checkpointer: Postgres (`langgraph-checkpoint-postgres`). `thread_id` =
story JIRA key.

## Tools and languages

Dev agent surface: `roles/dev_loop.py` → `agents/loop.py` (tool dispatch,
off-thread) → `tools/dev_tools.py` → path-guarded filesystem, edit, grep,
allowlisted shell, language analyze/test. Planner's surface is the same
shape with `tools/planner_tools.py`'s read-only tools instead. There is no
MCP hop in either: `make_dev_tools()`/`make_planner_tools()` hand their
`(functions, schemas)` pair straight to the loop.

**Language extension point:** `forge/tools/language_hooks.py`

1. Add a profile module (see `dart_tools.py`, `java_tools.py`, `vue_tools.py`).
2. Register in `_ensure_default_profiles_registered()`.
3. Add tests under `tests/test_*_tools.py`.

Do not special-case languages inside `safe_shell` / `dev_tools` — profiles
own analyze, test, verify, safe command prefixes, and prompt conventions.

## Reconciler and daemon

- `forge/orchestration/reconciler.py` is a **pure** function: snapshots in,
  `Intent` list out. No DB, no LLM, no network.
- Daemon (`forge/daemon/main.py`) applies intents. Runs and merges start as
  **background tasks** (`_launch`), up to `max_parallel_stories` (default 3)
  across every epic, and `poll_once` returns without waiting — a story that
  becomes ready when another merges starts on the next poll. A story is
  never in two tasks at once (the run registry, `claim_run`, covers merges
  too). Shutdown cancels them (`cancel_background_work`); each unwinds its
  story's status. Tests pass `poll_once(sm, wait=True)`.
- **Merge queue** (`_merge_story_into_epic`, one story at a time per epic;
  git side in `orchestration/integration.py`): a `ready_to_merge` story is
  (1) brought up to date with the epic branch (`update_story_from_epic`),
  (2) re-checked with the Tier 1 gate when anything new came in, and (3)
  fast-forwarded into it (`--ff-only`, so the epic branch only ever holds
  verified code). A conflict is left in the story's worktree and the story
  is sent back to dev with a brief naming the files and the stories that
  landed meanwhile (`_send_back_to_dev`: the finished thread is re-entered
  as if the gate had failed); a combined-code check failure the same way.
  Those files are added to dev's write scope for that pass
  (`integration_paths`). `stories.merge_attempts` caps it at
  `_MAX_MERGE_ATTEMPTS`; then `failed` / `failure_stage="merge"`, whose
  Retry re-attempts only the landing.
- **The epic's pull request** (`_finish_epic`): once every story is
  merged/done/skipped, the epic branch is pushed and one PR opened into the
  default branch per repo with changes (`epic_pull_requests`); its body
  lists each story, coverage gaps, already-satisfied declarations and the
  planner's clarification decisions. Phase `pr_open` is still polled —
  CI status and state are refreshed every cycle, in manual-run projects
  too — until a human merges it (`merged`) or closes it (`complete`).
  Without GitHub configured, or with nothing to propose, the epic ends
  `complete` with its branch ready locally. Story branches are never
  pushed.
- Manual epic gate (`epics.manual_gate`) parks the whole epic independently
  of the plan gate.
- The ticket-filter sweep (`forge/daemon/ticket_sync.py`) is a **second,
  slower loop** in the same process, deliberately not folded into
  `poll_once`: it hits someone else's API for a set that changes a few
  times a day, and a JIRA outage must not stall stories mid-run. Each
  project's sweep runs in `asyncio.to_thread` (blocking HTTP + sync
  SQLAlchemy) and one project's failure never ends the loop.
- Best-effort external side effects (JIRA QA comment, the epic's PR): log
  and continue — a failed epic PR push or API call is recorded on its
  `epic_pull_requests` row and retried next poll; neither fails a run.
- `stories.coverage_gap` (set when the verifier's only blocker was a
  coverage-threshold miss, not a real failure — see
  `forge/tools/vue_tools.py`'s coverage-only-failure detection) no longer
  stops a story: it merges, and the gap is called out in the epic's pull
  request.

## Spend caps

Three nested levels, all in USD and all treating **0 as unbounded**:

1. `story_max_budget_usd` — the outer bound on one story across planning,
   investigation, development and review. Enforced by
   `services/usage.py:effective_budget_usd`, which clamps each phase's own
   cap to what the story has left, and by a refusal to start a phase at
   all once nothing remains (`exhausted`) — a phase handed a cap of zero
   would read it as unbounded and do the opposite.
2. `planner_sdk_max_budget_usd` / `dev_sdk_max_budget_usd` — per loop.
3. Each explorer gets half the planner's effective remainder, split across
   the fan-out (`explorer.py:_explorer_budget`).

The four places a story reaches a model are all covered: planner and dev
raise their own role errors, and verifier/QA go through
`generation.chat_completion_json`, whose `GenerationError` the graph
already turns into a failed story a human can retry after raising the
ceiling.

**Limitation worth knowing:** spend is summed from `story_usage.cost_usd`,
which is None for any (provider, model) pair missing from
`llm/pricing.py` *unless the provider reports its own cost* (OpenRouter
does). A model with neither cannot be bounded at all. That is why a test
pins every shipped model default against the pricing table.

## Run telemetry

Two tables, both written as a run progresses and both keyed on `run_id`
(== story key == `thread_id`), both owned by `services/` rather than
StateManager (hard constraint #1's documented exception):

- `story_usage` — what a run **cost**: tokens, cache reads, cost and
  duration per (run, phase, model, provider). Sums only — the per-turn
  split, cache reads and writes included, is in each turn's `token`
  activity event (`services/usage.py`'s `turn_usage_meta`, dev and planner
  alike), which is where a run that stopped hitting the cache shows up.
- `story_run_outcomes` — how much work it **took**: gate runs and
  failures, verifier runs and rejections, dev passes/turns/terminal
  reason, planner turns, explorer count, final graph status. Written by
  the graph nodes and role modules themselves, incrementally, so a run
  that failed or was abandoned still leaves the counters it reached —
  those are the runs worth studying. `get_run_outcome` returns **None**,
  not zeros, for a run that predates the table: "not measured" and
  "measured as zero" are different claims and the UI must not conflate
  them.

`gate_failures` vs `verifier_rejections` is the pair to watch. Tier 1
exists to catch a broken build before the LLM reviewer is paid for one, so
it is working when the first is high and the second is low. If neither
ever fires for your repo and model, the retry apparatus is dead weight;
if the reviewer keeps rejecting builds the gate passed, the gate is
checking the wrong things.

There is **no eval harness** — nothing re-runs a fixed task set to score a
prompt or threshold change. These counters exist so that a real task set
and a real baseline can be assembled from merged stories later, instead of
tuning against invented tasks now.

## Config and migrations

- Config schema and UI fields: `forge/config/store.py` (`CONFIG_FIELDS`,
  `PROJECT_OVERRIDABLE_KEYS`).
- Project overrides: JIRA creds + approval gates only. The LLM provider/
  key / GitHub / models stay global. A project's ticket filter is **not** a config
  override — it is its own table (`project_ticket_filters`), since it has
  no global default to fall back to.
- Alembic: `alembic/versions/YYYYMMDD_NNNN_description.py`. Docstring must
  explain backfill/default semantics for existing rows.
- Adding a column: model → migration → StateManager mixin → keep backfill
  meaning stable.

LangGraph checkpointer creates its own tables on first use
(`AsyncPostgresSaver.setup()`).

## API surface (high level)

| Area | Prefix | Examples |
|------|--------|----------|
| Config | `/api/config` | Global settings CRUD |
| Projects | `/api/projects` | Projects (DELETE removes one and everything forge holds for it), repos (cloned from a URL or attached from a local folder — `forge/services/repo_clone.py`), overrides, ticket filter (+ `/preview`, `/sync`) |
| Epics | `/api/epics` | Sync from JIRA, assign repo, manual gate |
| Stories | `/api/stories` | Approve gate, set kind, get story, get plan |

There is no “start run” endpoint: sync epic (by key, or by the project's
ticket filter) → assign repo → daemon dispatches when reconciler says ready
→ human `POST .../approve` clears gates.

## UI

- `settings-ui/*.html` — three thin shells (projects list, settings,
  project detail), one per `forge/app.py` route. Each just mounts a React
  root (`<div id="root">` + one `<script type="module"
  src="/settings-ui/vendor/app-*.js">`); only the pre-paint theme script
  and the one `app.css` `<link>` stay as raw HTML — those must run before
  any JS bundle loads.
- `ui-src/src/` — the actual UI: TypeScript + React + shadcn/ui
  (Radix UI primitives, vendored — not installed via the shadcn CLI —
  under `components/ui/`, styled with Tailwind v4 utility classes) plus
  page-level components under `components/{projects,project-detail,
  settings,shared}/`. `components/ui/*` is the only place a Radix import
  belongs; everything else composes those. Icons come from `lucide-react`;
  toasts from `sonner` (`lib/toast.tsx` wraps it so call sites keep
  reading `showToast(message, appearance)`). Every dependency is public —
  see `ui-src/README.md` on why that matters. Dark mode keys off
  `[data-theme="dark"]` on `<html>` (not the `.dark` class shadcn
  scaffolds by default) so the existing pre-paint script and
  `lib/theme.ts` needed no changes — only `ui-src/src/styles/globals.css`'s
  `@custom-variant dark` selector does. Plus TanStack Query for all server
  state (polling via `refetchInterval`, cache invalidation after mutations
  instead of manual refetch calls) — with one deliberate exception: the
  story detail dialog's activity feed is pushed, not fetched.
  `useStoryActivity` (`hooks/useStories.ts`) holds an `EventSource` on
  `GET /api/stories/{key}/stream` for as long as the dialog is open; there
  is no query to cache or invalidate, and the effect cleanup closes the
  connection. No client-side router — each entry
  (`ui-src/src/entries/*.tsx`) is an independent root matching one HTML
  shell, same navigation model as before (plain `<a href>`/
  `window.location` between pages).
- `ui-src/build.mjs` bundles all three entry points (esbuild, built-in
  JSX/TSX support) into `settings-ui/vendor/app-*.js`, then shells out to
  the Tailwind CLI to compile `src/styles/globals.css` into
  `settings-ui/vendor/app.css` — esbuild has no CSS-transform story of its
  own, so Tailwind's CLI (not a bundler plugin) does that half of the
  build. The stylesheet is compiled standalone rather than imported from
  an entry, so the shells `<link>` one file instead of three bundled
  copies.
- **Do not commit** `settings-ui/vendor/` or `ui-src/node_modules/` — both
  are build output, regenerated by `npm run build` in seconds. The
  Dockerfile's `ui-build` stage runs that build itself, so the image is
  self-contained and `docker compose up --build` needs nothing prepared on
  the host.

## Testing conventions

- `tests/conftest.py`: `postgres_url` (testcontainers), `forge_db`,
  `git_repo`, `fake_agents` (patches all five graph entry points:
  planner, dev, deterministic gate, verifier, epic QA).
- **Never** call a real LLM provider / GitHub / live JIRA in
  unit/integration tests. Patch agent entry points on
  `forge.orchestration.graph` (names imported at module load), or fake
  `forge.llm.registry.get_provider` directly when testing a role module
  itself (see `tests/test_dev_loop.py`, `tests/test_generation.py`).
- Pure reconciler tests need no DB. Graph/daemon/API tests use Postgres.
- Prefer fakes at service boundaries over broad mocks.
- `tests/test_architecture.py` enforces the hard constraints below by
  walking the real import graph — provider-SDK isolation, `forge.db.models`
  access, reconciler purity, no language names in the shared tool layer.
  Adding a legitimate exception means adding it to that test's allow-list
  *and* to the constraint it belongs to, so the two never drift apart.

## Coding conventions

- `from __future__ import annotations` everywhere.
- Modern typing (`str | None`, `dict[str, Any]`); TypedDict for graph
  state; frozen dataclasses for reconciler intents.
- Pydantic models for API request bodies only.
- FastAPI routes mostly sync (sync SQLAlchemy StateManager); graph nodes
  and agent-role runners are async.
- Module docstrings are long and **why-oriented** — match that style when
  changing contracts.
- Fail loud for missing worktree / required QA context / missing config
  for a required op. Soft-fail optional remote notifications.

## Hard constraints (do not violate)

1. Use **StateManager** for epic/story/PR/project DB access — never import
   `forge.db.models` outside `forge/db/`, `forge/state/`, `forge/config/`
   (the config store owns `AppConfig`/`ProjectConfig` the same way
   StateManager owns the rest), and the run-telemetry pair
   `services/run_activity.py` + `services/usage.py` + their
   `services/activity_stream.py` fan-out. That exception list is
   enforced, not advisory — see `tests/test_architecture.py`.
2. Keep the **reconciler pure**.
3. Assemble QA context in the **daemon** so roles stay free of StateManager.
4. Framework owns JIRA/GitHub writes today; MCP (`forge/mcp/jira_server.py`,
   `github_server.py`) is the agent integration boundary for those tools,
   not a free-for-all write path from roles.
5. **Sub-tasks are never dispatched.**
6. **Do not recompute** `Story.kind` on epic re-sync — including the
   levels derived from it: a re-sync reads stored kinds for stories that
   already exist, so a human's override survives every later sweep.
7. Dev writes stay **plan-scoped** (`dev_tools` + `path_guard`).
8. Config from Postgres — not env (except `DATABASE_URL`).
9. Planner/dev run **only** through `forge/agents/loop.py`; verifier/qa
   **only** through `forge/services/generation.py`. No role constructs an
   `LlmProvider` or imports a provider SDK (`openai`, etc.) directly —
   always go through `forge/llm/registry.py:get_provider()`.
10. Do not add `.env` support for product settings.
11. `forge/llm/` is the only package that may import a provider SDK, and
    `openai_compat.py` is the only module in it that may import `openai`
    — every other adapter (present or future) gets its own module next to
    it, never a branch inside it.

## Not built yet (do not assume)

- Qdrant / RAG pre-seeding for planner context (the daemon does fold a
  cheap **codebase skeleton** — every source file's path and top-level
  declarations, `tools/codebase_skeleton.py` — into the planner brief
  via `_build_story_brief`, but that is a filesystem walk, not retrieval)
- JIRA/GitHub MCP wired into agent roles (servers exist; daemon uses
  services)
- A second `forge/llm/` provider adapter — `openai_compat.py` (OpenAI's
  own API, or any OpenAI-compatible endpoint via `llm_base_url`: Azure,
  OpenRouter, vLLM, Ollama, etc.) is the only one wired today. The port
  (`forge/llm/base.py`'s `LlmProvider`/`Capabilities`) is built to take a
  second one without touching `agents/loop.py` or any role.

## Where to change what

| Goal | Start here |
|------|------------|
| New language support | `tools/*_tools.py` + `language_hooks.py` |
| Sync / levels / kind | `services/epic_sync.py`, `services/story_kind.py` |
| Filter-based ingestion | `services/jira_filter.py`, `daemon/ticket_sync.py` |
| Which system tickets come from (source per project, neutral `Ticket`) | `services/ticket_source.py` — sync code calls `get_ticket_source(project_id)`, never `JiraService` directly |
| Live activity / streaming | `services/activity_stream.py`, `api/run_routes.py` |
| Graph routing / gates | `orchestration/graph.py`, `config/store.py` |
| Dispatch / PR / CI | `daemon/main.py`, `orchestration/integration.py` |
| Where a branch starts (existing work for a ticket) | `services/prior_work.py` (git), `daemon/prior_work_lookup.py` (when, storage, planner view) |
| Read-only repos and reference folders (what runs read, what plans may change) | `tools/reference_folder.py`, `tools/workspace.py`'s `Workspace.read_only`, `agents/roles/plan_gate.py`, `daemon/main.py`'s `_workspace_roots` |
| Agent behavior | `agents/roles/*` |
| Removing a ticket (rows, worktrees, branches, checkpoints — local only) | `daemon/ticket_removal.py`, `state/manager_removal.py`, `tools/worktree.py`'s `discard_worktree` |
| Deleting a project (its tickets, clones, settings — local only) | `daemon/project_removal.py`, `state/manager_project.py`'s `delete_project_records` |
| What the agents are told about a repo (its AGENTS.md, or one forge generates) | `services/project_guide.py`; routes in `api/project_routes.py` |
| Persistence API | `state/manager_*.py` + Alembic |
| UI | `ui-src/src/` (React/TS); rebuild with `cd ui-src && npm run build` |
| Tests | Mirror package under `tests/`; extend `fake_agents` if graph entry points change |

## Commit and PR hygiene

- Prefer **one logical module per commit** (tools, story-kind, agents,
  orchestration, daemon, UI, docs) rather than a single mixed dump.
- Do not commit secrets, `.env`, or `settings-ui/vendor/` / `ui-src/node_modules/`.
- Match existing commit style: short subject focused on why; body with
  concrete bullets when the change spans behavior contracts.
