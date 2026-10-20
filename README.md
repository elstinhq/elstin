# Elstin

Elstin turns a JIRA epic (or a set of GitHub Issues) into a pull request. It
runs on your own machine, in one process, keeping its state in one SQLite file.

You point it at a project's git repos and a ticket. For each story in the
ticket, an agent plans the change, you approve the plan, another agent writes
the code in an isolated git worktree, and a verifier checks it. Finished
stories are merged into an epic branch locally, and when they are all in,
Elstin opens one GitHub pull request. A human reviews and merges that PR.

It is a local, single-user tool: no Kubernetes, no multi-tenancy, no login.

- [How it works](#how-it-works)
- [What you need](#what-you-need)
- [Set it up](#set-it-up)
- [Run it as a Mac app](#run-it-as-a-mac-app)
- [Run it from source](#run-it-from-source)
- [Run it in Docker](#run-it-in-docker)
- [Configure it](#configure-it)
- [Your first run](#your-first-run)
- [Things to know before you point it at real work](#things-to-know-before-you-point-it-at-real-work)
- [Moving from the Postgres version](#moving-from-the-postgres-version)
- [Troubleshooting](#troubleshooting)
- [Develop](#develop)

## How it works

```
ticket (JIRA epic / GitHub issue)
   │
   ▼
planner ──► you approve the plan ──► dev ──► checks ──► verifier ──► merged into the epic branch
(reads the code)                     (writes code in    (build, tests,   (review against
                                      its own worktree)  plan coverage)   the plan)
                                                                              │
                        all stories merged ──► [optional: you approve] ──► one GitHub PR per repo
```

- **A deterministic reconciler decides what to do next.** The agents never
  choose which story runs; the daemon polls every 15 seconds and dispatches
  whatever is ready.
- **Stories run in parallel** when they do not depend on each other (3 at a
  time by default). A story waits only for the stories it depends on.
- **Every story gets its own git worktree** per repo it writes, so parallel
  work never collides and your own checkout is never touched.
- **A project can span several repos.** The approved plan decides which ones
  a story writes to.
- **Human gates.** Every new epic starts *paused* until you resume it. Each
  story's plan waits for your approval (on by default), and the planner may
  ask you clarifying questions. Optionally, the epic's pull request is also
  held until you say to push it.
- **A PO agent** can draft epics, stories and tasks (or trace a bug) from the
  code, Confluence and JIRA, for you to edit and publish.
- **Epic QA.** A ticket classified as QA (by its labels, issue type or title)
  is not coded: it waits for every dev story, then runs the whole test suite
  on the epic branch and writes a report.

[AGENTS.md](AGENTS.md) is the full architecture map.

## What you need

| You need | For | Notes |
|---|---|---|
| **A Mac (Apple silicon, macOS 15+)**, or **Python 3.11+ and Node 22**, or **Docker Desktop / Podman** | Running Elstin | Pick one of the [three ways to run it](#set-it-up). Docker or Podman is picked automatically. |
| **An LLM API key** | Every agent | OpenAI, or any OpenAI-compatible endpoint (Azure, OpenRouter, vLLM, Ollama, a corporate proxy). |
| **A GitHub token** | Cloning private repos over HTTPS, pushing the epic branch, opening the PR, reading GitHub Issues | Classic token: `repo` scope. Fine-grained token: Contents and Pull requests read/write on the repos, plus Issues if tickets come from GitHub. |
| **JIRA access** *(only if tickets come from JIRA)* | Reading epics and stories | Your Atlassian email, an [API token](https://id.atlassian.com/manage-profile/security/api-tokens), and the site URL, e.g. `https://yourco.atlassian.net`. |
| **A git repo to work on** | The code the agents change | A GitHub URL to clone, or a repo already on your disk. |

There is no database to install: Elstin keeps its state in one SQLite file it creates
itself.

## Set it up

### 1. Get the code

```bash
git clone git@github.com:elstinhq/elstin.git
cd elstin
```

### 2. Pick how to run it

All three keep their state in one SQLite file and behave the same.

| | Best for | |
|---|---|---|
| [**Mac app**](#run-it-as-a-mac-app) | Everyday use on a Mac | `packaging/build-app.sh`, then open `build/Elstin.app` |
| [**From source**](#run-it-from-source) | Working on Elstin, or using your own installed toolchains | `scripts/start-local.sh` |
| [**Docker**](#run-it-in-docker) | A server, or no local toolchain | `scripts/elstin.sh` |

### 3. Open it and configure it

From source and Docker serve **http://localhost:8000/** (the dashboard) and
**http://localhost:8000/settings**; the Mac app opens its own window.

There is **no `.env` file**. The only environment variable Elstin reads is
`DATABASE_URL` (the path of the SQLite file; Docker Compose sets it for you).
Every other setting is stored in that database and edited live in the browser: no
restart needed.

Go on to [Configure it](#configure-it).

## Run it as a Mac app

Elstin can also run as a native macOS app (Apple silicon, macOS 15 or later): a small
window around the same web UI, with the server bundled inside, so no Python, virtualenv
or Docker is needed. Build it from a checkout:

```bash
packaging/build-app.sh      # -> build/Elstin.app
```

It needs `uv`, Node and Xcode's command line tools. The app is signed ad hoc, which is
enough to run on the Mac that built it; it is not yet signed for distribution or
notarized, and there is no `.dmg`.

- **Your data** is the same SQLite file as running from source, so the app and a
  checkout share one database (and so cannot run at the same time).
- **Quitting** asks first if work is in flight; closing the window does not quit, since
  runs take hours. While anything runs, the Mac is kept awake and the Dock icon shows
  the count.
- **Tools** (`node`, `flutter`, `mvn`, ...) are found through your login shell's
  environment, so what works in your terminal works in the app.
- **Logs** are in `~/Library/Logs/Elstin/`.

How it is built, and how to test it, is in [packaging/README.md](packaging/README.md).

## Run it from source

Use this to work on Elstin itself, or to run it where your repos' toolchains
(JDK, Node, Flutter) are installed. You need **Python 3.11+**, **Node 22** (to
build the UI). Nothing else: the database is a file the app creates.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# The UI bundle is build output, not committed.
(cd ui-src && npm ci && npm run build)

# Creates and migrates the database on startup.
uvicorn elstin.app:create_app --factory --host 127.0.0.1 --port 8000
```

Then open http://localhost:8000/ and [configure it](#configure-it).

The database is `~/Library/Application Support/Elstin/elstin.db` on macOS
(`~/.local/share/elstin/elstin.db` elsewhere), with LangGraph's checkpoints in
`elstin.checkpoints.db` beside it. `DATABASE_URL=sqlite:////some/path.db` moves
it, for instance to try a branch on a copy of your data.

One difference from the Docker stack:

- **Attach host paths as they are**, with no bind mount.

The storage directories under *Git and storage* (worktrees, cloned repos, run
snapshots, JIRA attachments) default to folders beside the database, so from
source they are under `~/Library/Application Support/Elstin/` and in Docker under
`/data`. Point them elsewhere in Settings if you like.

Only one Elstin can use a database at a time (two would each pick up the same
stories): a second one stops with a message naming the first. That includes the
Mac app's backend, `elstin-app` — see [Run it as a Mac app](#run-it-as-a-mac-app).

The Docker stack keeps its own database in a volume, separate from this one, so
the two can run side by side if you give them different ports.

`scripts/start-local.sh` automates the steps above (venv, UI build, uvicorn).

## Run it in Docker

The whole stack in one container, with the database in a Docker volume. Good for a
server or a machine without a toolchain.

### Start it

```bash
scripts/elstin.sh
```

This is `docker compose up` with the rough edges taken off. It finds Docker or
Podman, builds the image (including the web UI), starts the app (which creates and
migrates its database), waits until it answers, and prints where to go. **The first run can take a few minutes** while the image builds; later
runs are quicker.

Plain `docker compose up --build` also works if you prefer it.

The dashboard is at **http://localhost:8000/** and settings at **http://localhost:8000/settings**.

### Stopping and restarting

```bash
scripts/stop.sh          # stop and remove the containers
scripts/stop.sh --keep   # stop them in place, for a faster restart
scripts/elstin.sh        # start again, where you left off
```

Neither stop command deletes anything: the database, worktrees, cloned repos
and `/data` all live in Docker volumes that survive. Only
`scripts/elstin.sh reset` deletes data, and it asks first.

### Day to day

```bash
scripts/elstin.sh            # build, start, wait until healthy (default)
scripts/elstin.sh status     # what is running, and whether the app is healthy
scripts/elstin.sh logs       # follow the app's logs
scripts/elstin.sh restart    # down + up
scripts/elstin.sh rebuild    # rebuild from scratch, ignoring the layer cache
scripts/elstin.sh shell      # a shell inside the app container
scripts/elstin.sh reset      # DESTRUCTIVE: delete the data volumes, database included (asks first)
scripts/elstin.sh --help     # the rest
```

The port can be changed with an environment variable:

```bash
ELSTIN_APP_PORT=9000 scripts/elstin.sh
```

**Updating:** `git pull`, then `scripts/elstin.sh`. It rebuilds the image on
every start, because the image bakes in the code and the migrations, and a
stale image can fail with a misleading "Can't locate revision" error. The app
migrates its database itself on startup.

## Configure it

Open **Settings** (`/settings`). The sections are in the order you need them.
The minimum to get a first run going is **bold**.

### Model provider — **required**

| Setting | What to enter |
|---|---|
| **OpenAI API key** | Your key. Saved in the database and shown masked afterwards. |
| OpenAI-compatible API base URL | Leave blank for OpenAI. Set it to use Azure, OpenRouter, vLLM, Ollama or a corporate proxy. |

The default models are `gpt-5.1` for the planner and dev agent and
`gpt-5.1-mini` for the verifier and QA. **If your endpoint does not serve those
names, change them** under *Planner*, *Dev agent* and *Review* (and *PO agent*
if you use it). The model fields offer the models your provider reports.

### GitHub — **required to open pull requests**

| Setting | What to enter |
|---|---|
| **GitHub token** | See [What you need](#what-you-need) for the scopes. |
| Allow elstin to comment on and close GitHub issues | Off by default. Only relevant for projects whose tickets are GitHub Issues. |

Without a token Elstin can still do everything locally: the epic ends
"ready locally" with its branch in your repo and no PR.

### Ticket sources — **required for JIRA projects**

| Setting | What to enter |
|---|---|
| **JIRA base URL** | `https://yourco.atlassian.net` |
| **JIRA account email** | The email on your Atlassian account. |
| **JIRA API token** | The token you created above. |
| Allow the framework to write to JIRA | Off by default. When on, Elstin posts the epic QA report back to the ticket as a comment. |
| Re-run interval for ticket filters | Seconds between sweeps of each project's saved ticket query (default 300, minimum 30). |

A project that takes its tickets from GitHub Issues needs only the GitHub token.

### Git and storage — recommended

| Setting | What to enter |
|---|---|
| Commit author name / email | Use the name and the email on your GitHub account so commits are credited to you. Blank means commits are authored by Elstin itself. Elstin is added as a co-author either way. |
| Worktrees / cloned repos / data directories | Leave the defaults in Docker (`/data/...`, which is a volume). Change them if you [run from source](#run-it-from-source). |
| Worktree retention | How many finished stories keep their checkout on disk (default 10, `0` keeps everything). |

### Workflow — know the defaults

| Setting | Default | Meaning |
|---|---|---|
| Automatically dispatch ready stories | on | Off means a person presses **Start** on each story. |
| Require plan approval before dev starts | on | You review every plan before any code is written. |
| Require approval before the epic's PR is opened | off | On means the branch waits locally until you choose **Open** or **Keep local**. |
| Stories run in parallel at most | 3 | Across all epics. |

### Spend and context — set a ceiling

`Maximum total spend per story` (and the per-agent limits on the Planner, Dev
and PO sections) default to **0, which means unbounded**. Set a per-story
limit in USD before leaving it unattended.

### Optional

- **Confluence** and the **PO agent** sections, if you want the PO agent to read
  Confluence and publish drafts as JIRA tickets (both off by default).
- **Tracing** sends full prompts, code and ticket text to LangSmith. Off by
  default; leave it off unless you mean it.

Most of these can be overridden **per project** (JIRA connection, gates,
models, model endpoint and key) from the project's own Settings page, so one
project can use a different model or JIRA site.

## Your first run

Use a small, low-risk ticket and a sandbox repo the first time.

1. **Create a project.** On the dashboard choose **New project**. Give it a
   name, then add the git repos it works on, in either of two ways:
   - **Clone a URL.** Elstin clones it into its own storage. Use an
     `https://github.com/...` URL: the container has no SSH keys, and your
     GitHub token is what authenticates the clone and the push.
   - **Attach a folder** that is already on your disk. Elstin works in place,
     only adding branches and worktrees to its `.git`, and never touches your
     checked-out files. Running in Docker, see
     [Attaching a local repo in Docker](#attaching-a-local-repo-in-docker).

   A folder holding several repos can be attached repo by repo. A repo can be
   marked **read-only** (read by every run, never written), and a plain folder
   can be added as a **reference** for context (docs, a CLAUDE.md about how the
   repos relate).
2. **Add a ticket.** Check the project's **Settings** to see whether its
   ticket source is JIRA (the default) or GitHub Issues. Then, in the
   project's **Tasks** view, choose **Add ticket** and enter a JIRA epic key
   (or a lone story), or a GitHub issue as `owner/repo#12` or its URL. Elstin
   pulls the epic and its stories in and works out which depend on which.
   Alternatively, save a **ticket filter** and Elstin ingests every matching
   ticket on a slow sweep.
3. **Resume the epic.** New epics start **paused**. Press **Resume** when you
   are ready for work to begin.
4. **Approve plans.** Each story is planned first. It appears under **Needs
   you** on the dashboard (and in the project's task list) when it wants a
   decision: a plan to approve, a clarifying question to answer, a branch to
   choose when earlier work already exists for the ticket.
5. **Watch it work.** Open a story to see its live activity feed, its plan
   (with a diagram) and its token usage and estimated cost. You can message a
   running agent ("use the helper that already exists") or **Cancel run**.
6. **Review the PR.** When every story has merged, Elstin pushes the epic
   branch and opens one pull request per repo it changed. You review and merge
   it on GitHub. If you turned on the PR gate, choose **Open** first.

Failed stories can be **retried** (resuming at dev or review when possible),
re-planned, or removed. Removing a ticket deletes its local worktrees,
branches and records. It never touches GitHub or JIRA.

### Letting the PO agent draft the work

Under a project's **Backlog**, **New** starts a PO session: describe what you
want, and the agent reads the code (and Confluence/JIRA if enabled) and drafts
an epic with its stories. Given an existing epic it either splits it into
stories or does a gap analysis. Edit the draft and chat with the agent; nothing
is created in JIRA unless you enable that in Settings and publish it. It can
also take a bug report and trace the cause in the code.

### Project pages

| Page | What it is for |
|---|---|
| **Tasks** | The epics and stories, their status, and everything waiting on you. |
| **Backlog** | PO sessions. |
| **Folders** | The project's repos: add, attach, make read-only, view or edit the generated guide for each. |
| **Lessons** | Mistakes mined from runs that needed retries, for you to accept into the project guide. Off by default. |
| **Settings** | Per-project overrides, the ticket filter, and deleting the project. |

### Attaching a local repo in Docker

The container sees nothing of your filesystem by default. To attach a repo you
already have, mount it in `docker-compose.yml` under the `app` service's
`volumes`, then attach the path **inside the container**:

```yaml
    volumes:
      - /Users/you/code/shop-app:/workspace/shop-app   # then attach /workspace/shop-app
```

Restart with `scripts/elstin.sh`. Running from source needs none of this: you
attach the host path as it is.

## Things to know before you point it at real work

- **Your code leaves the machine.** Prompts include ticket text, source code
  and tool output, and go to whichever LLM endpoint you configured. The dev
  agent can also search the web and fetch public pages (docs, package
  READMEs) by default; turn that off with *Let the dev agent search the web*
  in the Dev agent section. Private and internal addresses are always refused.
- **There is no login.** Anyone who can reach the port can use the app and
  spend your API budget. `docker-compose.yml` publishes port 8000 on all
  interfaces, so on shared or office networks keep a firewall on, or
  [run from source](#run-it-from-source) (it binds
  to `127.0.0.1`).
- **It spends money.** Runs are billed by your provider. Set a per-story
  budget and keep parallelism modest while you are learning what a typical
  story costs. A story's usage shows token counts and an estimated cost
  (estimated from a pricing table unless the provider reports one).
- **It opens real pull requests** on repos whose `origin` is on GitHub. For a
  repo with no GitHub `origin`, the epic ends complete locally and no PR is
  opened.
- **Docker has Python and git only.** Elstin has built-in build/test support
  for **Java** (Maven, Gradle), **Vue/Vite** and **Dart/Flutter** repos,
  detected from `pom.xml`, `build.gradle(.kts)`, `vite.config.*` and
  `pubspec.yaml`. Those toolchains (JDK, Node, Flutter) must exist on the
  machine that runs Elstin, and the Docker image does not contain them. To work
  on such a repo, [run from source](#run-it-from-source) on a machine that has
  them. Other stacks have no built-in profile yet (adding one is described
  under "Orientation for a first change" in [CLAUDE.md](CLAUDE.md)).
- **Nothing is written back to JIRA or GitHub unless you allow it.** The write
  switches are off by default. Epic QA's comment is the one thing the daemon
  writes back, and only with the write setting on.

## Moving from the Postgres version

Earlier versions kept their state in Postgres. Elstin now starts with an empty
SQLite database, so to keep your projects, tickets, settings and run history,
copy them across once, with the old stack stopped and no run in flight:

```bash
pip install "psycopg[binary]" langgraph-checkpoint-postgres   # only to read the old database
python scripts/migrate_postgres_to_sqlite.py --from-container code_forge-forge-postgres --dry-run
python scripts/migrate_postgres_to_sqlite.py --from-container code_forge-forge-postgres
```

`--from-container` takes the connection details from the running container, so no
password goes on the command line; `--from postgresql+psycopg://user:pass@host:port/db`
names a database directly. The dry run shows what would be copied and writes
nothing. The real run copies every table, and each story's latest checkpoint (its
plan, dev history and where a parked run stands), into an empty SQLite database
in one transaction: it all arrives or none of it does. The Postgres database is
only read, so it stays as a backup. The old database must be at its last
revision (start the old checkout once and it migrates itself), and the SQLite
one must be empty (`python scripts/clear_db.py` empties it).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "The Docker daemon is not running" / "Podman is installed but its machine is not running" | Start Docker Desktop, or `podman machine start`. |
| Port already in use | Another Elstin holds 8000. Stop it, or set `ELSTIN_APP_PORT`. |
| Nothing happens after adding a ticket | New epics start paused: press **Resume**. Also check the story's status, since a plan may be waiting on you under **Needs you**. |
| Runs fail straight away with a model or auth error | The API key, base URL or model names in Settings. Make sure the configured models exist on your endpoint. |
| The epic finishes but no PR appears | No GitHub token, the repo's `origin` is not on GitHub, or the PR gate is holding it for your **Open** decision. |
| Clone of a private repo fails | Use the `https://` URL with a GitHub token that can read it. The container has no SSH keys. |
| `Can't locate revision` on start | A stale image. Run `scripts/elstin.sh rebuild`. |
| Pages are blank when running from source | The UI bundle was not built: `cd ui-src && npm ci && npm run build`. |
| A build or test command is "not found" in a run | The repo's toolchain is missing from the machine running Elstin. See [Docker has Python and git only](#things-to-know-before-you-point-it-at-real-work). |

The app's logs (`scripts/elstin.sh logs`) are the first place to look, and each
story's activity feed shows what its agents did and why a step failed.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

- Pure-logic tests (`tests/test_reconciler.py`) need no database. Most other
  tests get their own copy of a SQLite database that was built by
  `alembic upgrade head`, so nothing needs to be running and no shared dev
  database is touched.
- Tests never make real LLM, GitHub or JIRA calls: `tests/conftest.py`'s
  `fake_agents` fixture and per-test fakes stand in at those boundaries.
- A new column or table needs an Alembic migration under
  `alembic/versions/`.
- There is no lint, format or typecheck tooling configured: match the
  surrounding style by hand.

### UI

The UI is one TypeScript/React app under `ui-src/src/` with its own
client-side routes (`components/App.tsx`), mounted into the single HTML shell
`settings-ui/app.html`. Components are [shadcn/ui](https://ui.shadcn.com/)
source vendored under `ui-src/src/components/ui/`, styled with Tailwind. Every
dependency is public, so the build needs no registry credentials, and the
Docker image builds the UI itself. `settings-ui/vendor/` is gitignored build
output. After changing anything under `ui-src/src/`:

```bash
cd ui-src
npm ci
npm run build        # -> settings-ui/vendor/app.js
npm run typecheck    # tsc --noEmit
```

### Where things live

| Path | What |
|---|---|
| `elstin/orchestration/` | The pure reconciler and the LangGraph run |
| `elstin/agents/` | Planner, dev, verifier, QA and PO roles, and the shared agent loop |
| `elstin/daemon/` | The poll loop, ticket sweep, merge queue and the epic's PR |
| `elstin/services/` | JIRA and GitHub clients, ticket sync, repo clone/attach |
| `elstin/tools/` | Agent tools and per-language build/test profiles |
| `elstin/state/`, `elstin/db/` | The only database access, and the models |
| `elstin/config/` | Settings stored in the database |
| `ui-src/`, `settings-ui/` | The web UI source, and its shell and build output |

Read [AGENTS.md](AGENTS.md) before a non-trivial change: it lists the layering
rules (for example, only `state/` and `db/` touch the database models, and the
reconciler stays pure) and a "where to change what" table.
[CLAUDE.md](CLAUDE.md) has the commands and testing conventions.

## What's real vs. not yet ported

Everything in `elstin/` is real and tested against SQLite and git, except the
node bodies' RAG context pre-seeding: `elstin/agents/roles/planner_loop.py`'s
agent still explores the target codebase live via its own read/grep/find tools
every run (real), but does not yet get a pre-computed head start from a
Qdrant-backed RAG pipeline or prior-branch discovery. See that module's
docstring for the reasoning and what building it out would take.

## Feedback

This is early. If you try it, tell me where it fell over: a plan that missed
the point, a story that got stuck, a confusing screen, a setting that was not
obvious. The story's activity feed and `scripts/elstin.sh logs` make good
bug reports.
