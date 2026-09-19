# AGENTS.md — Code Forge

Guidance for AI agents and humans working in this repository. Prefer this
file over inventing architecture: the module docstrings are normative, and
this document is the map of where those contracts live.

## What this project is

**code_forge** is a local, single-tenant JIRA-epic-to-PR platform. A
deterministic reconciler decides what to do next; a LangGraph loop runs
planner → dev → verifier (or epic QA) per story in an isolated git
worktree; the daemon opens GitHub PRs, waits for CI, and merges into a
shared epic integration branch. Human gates sit at plan review, PR open,
and PR review by default.

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
for `/healthz`, and adds `down`/`logs`/`status`/`rebuild`/`psql`/`reset`.
Plain `docker compose up` still works and nothing in `forge/` depends on
the script.

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
                                                ├── ParkForGate
                                                └── MergeIntegrationBranch / PR + CI

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
| `services/` | JIRA/GitHub clients, epic sync, ticket filters, story_kind, generation (verifier/QA's single-shot LLM call), repo cloning, run activity + its live fan-out, run telemetry (cost **and** run shape) | Bypass StateManager for persistence |
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
- **Commit, PR, CI and merge are per repo.** One `PullRequest` per
  (story, repo); the CI gate is the *conjunction* of every PR, so the UI
  half cannot merge while the API half is red. The integration branch NAME
  stays per epic, giving a same-named branch in each repo, which is what
  keeps a cross-repo epic reviewable as one unit; epic QA diffs all of
  them.
- Plan scope still authorises every write. A writable repo is not a
  free-for-all: the path must be in the plan, prefix included.
- A single-repo project is byte-identical to before — no prefixes, no note
  in any tool description, and `make_dev_tools` returns the shared
  `TOOL_SCHEMAS` object rather than a patched copy.

**The trade-off, stated plainly:** a story spanning two repos produces two
branches and two pull requests, and git has no transaction across them, so
it can land in one and fail in the other. Forge makes that visible (every
PR must be green before the merge step) rather than pretending otherwise.

Migrations: `20260919_0018_story_project_repo.py`,
`20260919_0019_multi_repo_stories.py`.

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
  `forge/services/jira_filter.py`; a sweep **only ever adds** — new roots
  are ingested, and roots forge already has are **re-walked** through the
  same idempotent `sync_epic_from_jira` so child stories added in JIRA
  after first ingest still arrive (`create_epic`/`create_story` no-op on
  existing rows, so a story mid-run is never touched). Sub-tasks, children
  of other matches, and keys owned by another project are skipped, and
  nothing is unsynced when a ticket stops matching. Ingested epics land
  paused and folder-less, exactly like "Add ticket".
- Migrations: `alembic/versions/20260817_0007_story_kind.py`,
  `20260817_0008_story_parent.py`.

### Levels (waves)

Topological sort on JIRA blocks/is-blocked-by among **dev** stories; QA
stories are forced to the final level. Same-level stories are ready
together; a level advances when all members are terminal
(`merged` / `done` / `skipped`).

## Agent roles

| Role | Module | Runtime | Notes |
|------|--------|---------|-------|
| Planner | `planner.py` + `planner_loop.py` | `agents/loop.py` (tool-driven) | `tools/planner_tools.py`'s read-only tools only; structured JSON plan via the provider's native structured output |
| Explorer | `explorer.py` | `agents/loop.py`, N concurrent sub-loops | Read-only recon *inside* the planner step, not a graph node. Scope -> fan out -> fold findings into the plan prompt. Prompt adapted from Aster's `scout` agent (Apache-2.0) |
| Dev | `dev.py` + `dev_loop.py` | `agents/loop.py` (tool-driven) | **Only** tools from `tools/dev_tools.py`'s `make_dev_tools()` — handed to the loop directly, no MCP layer |
| Verifier | `verifier.py` | Tier 1 deterministic gate (`run_deterministic_gate`, run by the graph's own `gate` node before the verifier is ever entered) + `generation.chat_completion_json` review | Language-specific `compare_to_plan` via hooks; `format_gate_feedback` turns a gate failure into dev's next user message; `_VERIFIER_JSON_SCHEMA` constrains the review |
| QA | `qa.py` (`run_epic_qa`) | `generation` + full test suite | Epic-level validation ticket; suite failure fails the run, subjective LLM verdict does not. `QA_JSON_SCHEMA` constrains the report, since `format_qa_report_comment` is its only consumer and reads specific keys |

**Planner exploration** (`agents/roles/explorer.py`): before the plan
loop, a scoping call picks up to `planner_explorer_max_agents` independent
investigation areas and one read-only sub-agent answers each, concurrently.
The point is context isolation, not speed: a file the planner reads itself
stays in its history and is re-sent every later turn, whereas a sub-agent's
reads die with its throwaway context and only a short report comes back.
Everything about it is best-effort — an empty scope, a failed scope, or
every explorer returning nothing all degrade to "plan without a head
start". Sub-agents get the read-only trio only, so there is no write path
and no way to spawn another one. Skipped on a clarification round-trip.
Their tokens roll up into the planner's `story_usage` row via the same
`on_turn` hook, and their spend caps are carved out of
`planner_sdk_max_budget_usd` rather than each inheriting the whole cap.

**Agent loop mechanics** (`forge/agents/loop.py`, model-agnostic — the
same loop drives GPT, DeepSeek, or a local vLLM/Ollama endpoint):

- Compaction is **tombstoning, not summarization**: tool results older
  than the last N turns are replaced in place with a fixed string; no
  message is removed or reordered, no LLM call is made, and the prompt
  prefix stays byte-stable for provider prefix caching.
- Consecutive **read-only** tool calls (`tools/dev_tools.py` /
  `tools/planner_tools.py`'s `READ_ONLY_TOOLS`) run concurrently; every
  other tool runs alone, in order. A new tool is mutating until declared
  otherwise.
- **Repeat-call guard**: an identical (name, args) call in the consecutive
  turn is intercepted and answered with an is_error nudge; resets after any
  mutating call.
- **Idle-turn budget** (dev only, `dev_sdk_max_idle_turns`): N consecutive
  tool turns with no `PROGRESS_TOOLS` call end the run as a soft stop
  (`no_progress`), same handling as a turn cap.
- **Trailing reminder** (`LoopConfig.trailing_reminder`): a callable
  re-evaluated each turn whose text is appended to the *request* as a
  final user message and never stored in `history`. That is what keeps it
  cheap — it sits after every cached byte, so it costs one short message
  per turn instead of bloating history and breaking the prefix. Dev uses
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
  config default against the table for this reason.
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
shape with `tools/planner_tools.py`'s read-only trio instead. There is no
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
- Daemon (`forge/daemon/main.py`) applies intents. Phase 1 dispatches
  stories **sequentially** per poll even though the reconciler can emit a
  ready set (parallelization is future work).
- Manual epic gate (`epics.manual_gate`) parks the whole epic independently
  of plan/PR gates.
- The ticket-filter sweep (`forge/daemon/ticket_sync.py`) is a **second,
  slower loop** in the same process, deliberately not folded into
  `poll_once`: it hits someone else's API for a set that changes a few
  times a day, and a JIRA outage must not stall stories mid-run. Each
  project's sweep runs in `asyncio.to_thread` (blocking HTTP + sync
  SQLAlchemy) and one project's failure never ends the loop.
- Best-effort external side effects (JIRA QA comment, GitHub PR open/merge):
  log and continue; do not fail the run for those alone.
- `stories.coverage_gap` (set when the verifier's only blocker was a
  coverage-threshold miss, not a real failure — see
  `forge/tools/vue_tools.py`'s coverage-only-failure detection) forces the
  pr_open_gate on regardless of `require_pr_open_approval`: a story that
  didn't fully verify always needs a human before its PR opens, even on a
  project that has that gate turned off.

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
`llm/pricing.py`. A model with no price row therefore cannot be bounded at
all. That is why a test pins every shipped model default against the
pricing table.

## Run telemetry

Two tables, both written as a run progresses and both keyed on `run_id`
(== story key == `thread_id`), both owned by `services/` rather than
StateManager (hard constraint #1's documented exception):

- `story_usage` — what a run **cost**: tokens, cache reads, cost and
  duration per (run, phase, model, provider).
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
| Projects | `/api/projects` | Projects, repos (cloned from a URL — `forge/services/repo_clone.py`), overrides, ticket filter (+ `/preview`, `/sync`) |
| Epics | `/api/epics` | Sync from JIRA, assign repo, manual gate |
| Stories | `/api/stories` | Approve gate, set kind, get story |

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
- Concurrent per-story LangGraph dispatch in the daemon
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
| Live activity / streaming | `services/activity_stream.py`, `api/run_routes.py` |
| Graph routing / gates | `orchestration/graph.py`, `config/store.py` |
| Dispatch / PR / CI | `daemon/main.py`, `orchestration/integration.py` |
| Agent behavior | `agents/roles/*` |
| Persistence API | `state/manager_*.py` + Alembic |
| UI | `ui-src/src/` (React/TS); rebuild with `cd ui-src && npm run build` |
| Tests | Mirror package under `tests/`; extend `fake_agents` if graph entry points change |

## Commit and PR hygiene

- Prefer **one logical module per commit** (tools, story-kind, agents,
  orchestration, daemon, UI, docs) rather than a single mixed dump.
- Do not commit secrets, `.env`, or `settings-ui/vendor/` / `ui-src/node_modules/`.
- Match existing commit style: short subject focused on why; body with
  concrete bullets when the change spans behavior contracts.
