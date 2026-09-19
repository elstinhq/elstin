# code_forge

A local, single-tenant JIRA-epic-to-PR agent platform: a LangGraph
planner/dev/verifier/qa loop with activity streaming, wrapped in a
disciplined orchestration model (a deterministic reconciler, Postgres-backed
state, MCP as the integration boundary, per-story worktree isolation,
human-approval gates, CI-aware merge gating) — trimmed to run on a laptop
with `docker compose up`, no Kubernetes, no multi-tenancy, no Teams bot.

The daemon polls for ready work, drives each story through a real
planner → dev → verifier → QA loop (forge's own agent loop —
`forge/agents/loop.py` — against an OpenAI-compatible LLM provider) in its
own git worktree, opens a real GitHub PR when implementation completes,
waits for CI to pass, and merges into a shared epic integration branch —
with a human approval gate at both the plan-review and PR-review steps by
default (configurable).

## Run locally

```bash
scripts/forge.sh
```

That is `docker compose up` with the rough edges taken off: it finds your
container engine (Docker or Podman), rebuilds the image so the running
code is the code you have, waits until the app actually answers, and
prints where to go. `docker compose up` still works if you prefer it.

To stop:

```bash
scripts/stop.sh          # stop and remove the containers
scripts/stop.sh --keep   # stop them in place, for a faster restart
```

Neither deletes anything: the Postgres, worktree, cloned-repo and `/data`
volumes survive, and `scripts/forge.sh up` picks up where you left off.
Other commands: `stop`, `down`, `logs`, `status`, `rebuild`, `psql`,
`reset` (the one that does delete data, and asks first), and `--help` for
the rest. Ports follow `FORGE_APP_PORT` / `FORGE_PG_PORT`.

Then open **http://localhost:8000/** to watch epics/stories and
approve gated steps, or **http://localhost:8000/settings** to fill in your
JIRA URL/credentials (or, for a project that pulls from GitHub Issues, just the GitHub token), GitHub org/token, and OpenAI API key (or an
OpenAI-compatible endpoint's key + base URL — Azure, OpenRouter, vLLM,
Ollama, etc.). Nothing is read from a `.env` file — `DATABASE_URL` (wired
automatically by `docker-compose.yml`) is the only process environment
variable code_forge needs; every other setting lives in Postgres and is
edited live from that page, no restart required.

### UI

`settings-ui/` is three thin HTML shells, each mounting a React root built
from `ui-src/src/`. UI components are the app's own (`ui-src/src/ds/`) over
a single stylesheet of design tokens and utilities — every dependency is
public, so the build needs no registry credentials and `docker compose up
--build` builds the frontend itself. `settings-ui/vendor/` is gitignored
build output; to rebuild by hand:

```bash
cd ui-src
npm install
npm run build
```

`docker compose up` (without `--build`, against an already-built image, or
after you've already run the UI build once) works without Node at all —
the Dockerfile just copies whatever's in `settings-ui/vendor/`.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up postgres -d
export DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
alembic upgrade head
pytest
```

Pure-logic tests (`tests/test_reconciler.py`) need no database. DB-backed
tests use `testcontainers-postgres` and spin up their own ephemeral
Postgres — no shared dev DB required. Tests never make real LLM / GitHub
calls — `tests/conftest.py`'s `fake_agents` fixture and per-test fake
service classes stand in at those boundaries.

## What's real vs. not yet ported

Everything in `forge/` is real and tested against Postgres/git, except the
node bodies' RAG context pre-seeding: `forge/agents/roles/planner_loop.py`'s
agent still explores the target codebase live via its own read/grep/find tools every
run (real), but doesn't yet get a pre-computed head start from a
Qdrant-backed RAG pipeline or prior-branch discovery — see that module's
docstring for the reasoning and what building it out would take.
