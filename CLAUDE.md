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
(or epic QA) loop runs per story in an isolated git worktree — independent
stories in parallel, a story waiting only for the stories it depends on —
each finished story is verified and merged into the epic branch locally,
and once all have merged the epic opens one GitHub pull request. Runs as one process (`docker compose up`) with
SQLite-backed state — one file the app creates and migrates itself, nothing to
install or start — no Kubernetes, no multi-tenancy, no `.env` file (the only
process env var is an optional `DATABASE_URL=sqlite:////path/to/elstin.db`;
everything else is configured live at `/settings` and stored in the database).

## Commands

Local dev setup (no Docker, nothing else to install):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn elstin.app:create_app --factory   # creates and migrates the database on startup
```

The database is one SQLite file, `~/Library/Application Support/Elstin/elstin.db`
on macOS (`~/.local/share/elstin/` elsewhere) — your real data when you run from
a checkout. `DATABASE_URL=sqlite:////some/path.db` puts it elsewhere, e.g. to
try a branch on a copy; a URL for any other database is refused. The server
runs `alembic upgrade head` itself before it serves, so a branch with a new
migration changes that file when you start it. LangGraph's checkpoints live in
`elstin.checkpoints.db` beside it.

Data from the old Postgres-backed version is copied across once with
`scripts/migrate_postgres_to_sqlite.py` (`--dry-run` first; see the README). It
needs `psycopg[binary]` and `langgraph-checkpoint-postgres`, which are not
dependencies any more — install them by hand to run it.

`elstin-app` (`python -m elstin.launcher`) is the entry point the macOS app's
window starts instead of uvicorn: a free port on 127.0.0.1, a per-launch access
token, the log in `~/Library/Logs/Elstin/`, and it exits with its parent. It
prints `ELSTIN_READY {port, token, pid}` on stdout (see `elstin/launcher.py`);
run it by hand with `--no-exit-with-parent` when poking at it from a terminal. The
token is checked by `elstin/api/access_guard.py`, which only the launcher turns on.

`packaging/build-backend.sh` freezes that launcher into a self-contained
`build/dist/elstin-backend/` (PyInstaller, a standalone Python 3.12, dependencies from the
hash-pinned `packaging/requirements.lock`), runs its `--self-test` and inspects every native
file for the macOS it needs — see `packaging/README.md`. `elstin-app --self-test` is also
worth running after adding a dependency or a lazily-imported module: it is what notices a
freezer missing it.

`packaging/build-app.sh` builds `build/Elstin.app`: the native Swift window (`packaging/app/`,
SwiftPM, no dependencies; `swift test --package-path packaging/app --scratch-path build/swift`)
around that frozen backend. The window asks `GET /api/status` what is running (to warn before
quitting and keep the Mac awake) and starts the backend with `--prefer-port` — see
`packaging/README.md`.

`scripts/start-local.sh` does the venv, dependency and UI-bundle setup and then
runs the server in the foreground:

```bash
scripts/start-local.sh   # uvicorn from .venv on http://localhost:8000/
```

Run the full stack instead:

```bash
scripts/elstin.sh
# Dashboard: http://localhost:8000/
# Settings:  http://localhost:8000/settings
```

`scripts/elstin.sh` wraps `docker compose` (or `podman compose`) with
engine detection, an image rebuild on every `up` — the image bakes in
`elstin/`, `alembic/` and the UI bundle, so a stale one runs code you no
longer have and fails migrations with a misleading "Can't locate
revision" — a health wait, and `stop`/`down`/`logs`/`status`/`rebuild`/
`shell`/`reset` subcommands. `scripts/stop.sh` is the stop half (`--keep`
stops the containers without removing them); both leave every volume
alone, so only `reset` loses data. Plain `docker compose up` still works.

Tests:

```bash
pytest                                  # full suite
pytest tests/test_reconciler.py         # one file — pure logic, no DB needed
pytest tests/test_daemon.py::test_name  # one test
```

There are no pytest markers separating unit vs. integration tests — DB-backed
tests (most of them) pull in the `database_url` fixture: each test gets its own
copy of a database that `alembic upgrade head` built once per session, so the
schema under test is the migrated one, and nothing external (no Docker) is
needed. Only `test_reconciler.py`-style pure-logic tests skip the DB.

No lint/format/typecheck tooling is configured in this repo (no ruff,
black, or mypy) — match existing style by hand.

New DB column/table: add an Alembic migration under
`alembic/versions/YYYYMMDD_NNNN_description.py` (docstring must explain
backfill/default semantics for existing rows). SQLite cannot ALTER most things in
place, so write it with `op.batch_alter_table` (it rebuilds the table); the
app applies it on its next start, or run `alembic upgrade head`. `tests/test_migrations.py`
fails when the models and the migrations disagree.

One elstin per database: the app takes an exclusive lock on `elstin.db.lock` for as
long as it runs (`elstin/db/lock.py`), so a second one refuses to start. The
storage directories (worktrees, cloned repos, baselines, JIRA attachments) default
to folders beside the database file; read them with `storage_dir(key)`
(`elstin/config/store.py`), never a hard-coded `/data/...`.

Database rules that SQLite imposes (details in `elstin/db/engine.py`): every
transaction takes the write lock when it begins, so keep a `session_scope()` short
and never do network, subprocess or model calls inside one; scopes do not nest;
and store timestamps timezone-aware — use the `UtcDateTime` column type
(`elstin/db/types.py`), never a bare `DateTime`.

UI changes: the UI is one TypeScript/React app under `ui-src/src/`
with its own client-side routes (`components/App.tsx`, router in
`lib/router.tsx` — no dependency), mounted into the one thin HTML shell
`settings-ui/app.html`, which `elstin/app.py` serves for every page URL. A
new view is an entry in the route table; the server needs nothing.
After editing anything under `ui-src/src/`, rebuild:

```bash
cd ui-src && npm install && npm run build   # -> settings-ui/vendor/app.js
npm run typecheck                           # tsc --noEmit, not wired into CI
```

Every frontend dependency is public (react, react-dom,
`@tanstack/react-query`, Radix UI, Tailwind, lucide-react, sonner,
`@yogesharc/thinking-orbs` for the PO agent's working indicator,
`mermaid` for the diagrams in a story's plan — loaded on demand, so it is a
separate chunk under `settings-ui/vendor/chunks/`, not part of `app.js` —
esbuild, typescript) — no registry credentials, and the Dockerfile's
`ui-build` stage runs this same build, so `docker compose up --build`
needs no prior local build. Keep it that way: a dependency requiring
registry access takes the whole frontend build offline for CI and fresh
clones. The output (`settings-ui/vendor/`) is gitignored build product.

UI primitives are shadcn/ui — vendored source (not the shadcn CLI, which
fetches components from an external registry at add-time) under
`ui-src/src/components/ui/`, styled with Tailwind v4 utility classes over
the CSS variables in `ui-src/src/styles/globals.css`. Nothing under
`components/` outside `components/ui/` imports a Radix
package directly — only `components/ui/*` does. `build.mjs` shells out to
the Tailwind CLI to compile `globals.css` into `settings-ui/vendor/app.css`
(esbuild bundles the JS; it has no CSS-transform story of its own).
`settings-ui/app.html` stays plain HTML: a pre-paint theme script and one
stylesheet `<link>`, both of which must run before the JS bundle.

## Testing conventions specific to this repo

- Never call a real LLM provider / GitHub / live JIRA in tests.
  `tests/conftest.py`'s `fake_agents` fixture monkeypatches the five
  graph entry points (planner, dev, the Tier 1 deterministic gate,
  verifier, epic QA) on `elstin.orchestration.graph` (imported by name at
  module load, so patch them there, not at their definition site); a role
  module tested directly instead fakes `elstin.llm.registry.get_provider`.
- `git_repo` fixture (`tests/conftest.py`) gives a real repo with an
  `origin` remote — worktree code assumes `origin/<default>` exists, so
  tests should too.
- Prefer fakes at service boundaries (JIRA/GitHub clients, `generation`)
  over broad mocks.

## Orientation for a first change

1. Figure out which package owns it via AGENTS.md's package map and "Where
   to change what" table — this codebase enforces strict layering (e.g.
   only `state/`+`db/` may import `elstin.db.models`; the reconciler must
   stay pure; planner/dev run only through `elstin/agents/loop.py`, never a
   hand-rolled second chat loop or a direct provider-SDK call).
2. Adding a language: `elstin/tools/*_tools.py` (see `vue_tools.py` /
   `java_tools.py` for the current shape) + register in
   `elstin/tools/language_hooks.py`'s `_ensure_default_profiles_registered()`
   + tests under `tests/test_*_tools.py`. Never special-case a language
   name inside `safe_shell`/`dev_tools` directly.
3. Changing sync/dispatch behavior: `elstin/services/epic_sync.py` (levels,
   parent/sub-task hierarchy) and `elstin/services/story_kind.py`
   (dev-vs-qa classification) are the two places that decide what gets
   worked on and in what order — read both before changing either.
   *Which* tickets arrive in the first place is a third place:
   `elstin/services/jira_filter.py` (a project's saved JIRA query, compiled
   to JQL) plus `elstin/daemon/ticket_sync.py` (the slow background sweep
   that re-runs it). A sweep ingests new roots and **re-walks** roots
   elstin already has (and every in-progress epic, matched or not) so stories
   added later still arrive; nothing is ever unsynced or reordered. The
   re-walk also reconciles content and closure with the source — see
   `elstin/services/external_sync.py`: text changed under a plan is *held*
   for a human, never rewritten; closed/deleted tickets stop being
   dispatched.
