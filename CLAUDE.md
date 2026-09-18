# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read AGENTS.md first

[AGENTS.md](AGENTS.md) is the normative architecture map for this repo —
package responsibilities, story-kind/hierarchy rules, agent roles, the
reconciler/daemon contract, hard constraints, and "where to change what."
Read it before making non-trivial changes; this file only adds the
commands and orientation AGENTS.md doesn't cover.

## What this is

A local, single-tenant JIRA-epic-to-PR agent platform: a deterministic
reconciler decides what to do next, a LangGraph planner → dev → verifier
(or epic QA) loop runs per story in an isolated git worktree, and the
daemon opens GitHub PRs, waits for CI, and merges into a shared epic
integration branch. Runs as one process (`docker compose up`) with
Postgres-backed state — no Kubernetes, no multi-tenancy, no `.env` file
(the only process env var is `DATABASE_URL`; everything else is configured
live at `/settings` and stored in Postgres).

## Commands

Local dev setup (no Docker for the app itself, Postgres via Docker):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up postgres -d
export DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
alembic upgrade head
```

Run the full stack instead:

```bash
docker compose up
# Dashboard: http://localhost:8000/
# Settings:  http://localhost:8000/settings
```

Tests:

```bash
pytest                                  # full suite
pytest tests/test_reconciler.py         # one file — pure logic, no DB needed
pytest tests/test_daemon.py::test_name  # one test
```

There are no pytest markers separating unit vs. integration tests — DB-backed
tests (most of them) pull in the `postgres_url` fixture, which spins up a
real ephemeral Postgres via `testcontainers-postgres` per session; only
`test_reconciler.py`-style pure-logic tests skip the DB. No Docker daemon
running means those DB-backed tests will fail to start a container.

No lint/format/typecheck tooling is configured in this repo (no ruff,
black, or mypy) — match existing style by hand.

New DB column/table: add an Alembic migration under
`alembic/versions/YYYYMMDD_NNNN_description.py` (docstring must explain
backfill/default semantics for existing rows), then `alembic upgrade head`.

UI changes: the settings UI is a TypeScript/React app under `ui-src/src/`
(three independent React roots — projects list, settings, project detail —
mounted into the three thin HTML shells at `settings-ui/*.html`; no
client-side router, matching FastAPI's existing one-route-per-page setup
in `forge/app.py`). After editing anything under `ui-src/src/`, rebuild:

```bash
cd ui-src && npm install && npm run build   # -> settings-ui/vendor/app-*.js
npm run typecheck                           # tsc --noEmit, not wired into CI
```

Every frontend dependency is public (react, react-dom,
`@tanstack/react-query`, Radix UI, Tailwind, lucide-react, sonner,
esbuild, typescript) — no registry credentials, and the Dockerfile's
`ui-build` stage runs this same build, so `docker compose up --build`
needs no prior local build. Keep it that way: a dependency requiring
registry access takes the whole frontend build offline for CI and fresh
clones. The output (`settings-ui/vendor/`) is gitignored build product.

UI primitives are shadcn/ui — vendored source (not the shadcn CLI, which
fetches components from an external registry at add-time) under
`ui-src/src/components/ui/`, styled with Tailwind v4 utility classes over
the CSS variables in `ui-src/src/styles/globals.css`. Nothing under
`components/{projects,project-detail,settings,shared}/` imports a Radix
package directly — only `components/ui/*` does. `build.mjs` shells out to
the Tailwind CLI to compile `globals.css` into `settings-ui/vendor/app.css`
(esbuild bundles the JS; it has no CSS-transform story of its own).
`settings-ui/*.html` stay plain HTML: a pre-paint theme script and one
stylesheet `<link>`, both of which must run before the JS bundle.

## Testing conventions specific to this repo

- Never call a real LLM provider / GitHub / live JIRA in tests.
  `tests/conftest.py`'s `fake_agents` fixture monkeypatches the five
  graph entry points (planner, dev, the Tier 1 deterministic gate,
  verifier, epic QA) on `forge.orchestration.graph` (imported by name at
  module load, so patch them there, not at their definition site); a role
  module tested directly instead fakes `forge.llm.registry.get_provider`.
- `git_repo` fixture (`tests/conftest.py`) gives a real repo with an
  `origin` remote — worktree code assumes `origin/<default>` exists, so
  tests should too.
- Prefer fakes at service boundaries (JIRA/GitHub clients, `generation`)
  over broad mocks.

## Orientation for a first change

1. Figure out which package owns it via AGENTS.md's package map and "Where
   to change what" table — this codebase enforces strict layering (e.g.
   only `state/`+`db/` may import `forge.db.models`; the reconciler must
   stay pure; planner/dev run only through `forge/agents/loop.py`, never a
   hand-rolled second chat loop or a direct provider-SDK call).
2. Adding a language: `forge/tools/*_tools.py` (see `vue_tools.py` /
   `java_tools.py` for the current shape) + register in
   `forge/tools/language_hooks.py`'s `_ensure_default_profiles_registered()`
   + tests under `tests/test_*_tools.py`. Never special-case a language
   name inside `safe_shell`/`dev_tools` directly.
3. Changing sync/dispatch behavior: `forge/services/epic_sync.py` (levels,
   parent/sub-task hierarchy) and `forge/services/story_kind.py`
   (dev-vs-qa classification) are the two places that decide what gets
   worked on and in what order — read both before changing either.
   *Which* tickets arrive in the first place is a third place:
   `forge/services/jira_filter.py` (a project's saved JIRA query, compiled
   to JQL) plus `forge/daemon/ticket_sync.py` (the slow background sweep
   that re-runs it). A sweep only ever adds: new roots are ingested, roots
   forge already has are **re-walked** so stories added to them in JIRA
   later still arrive, and nothing is ever unsynced, paused, or reordered.
