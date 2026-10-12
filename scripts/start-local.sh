#!/usr/bin/env bash
#
# Run elstin on the host: Postgres in a container, the server from your
# own checkout.
#
# `scripts/elstin.sh` runs the whole stack in Docker, where the app is a
# baked image. This is the other mode — the one for working on elstin's
# code — and it does the setup CLAUDE.md lists by hand:
#
#   * starts the existing Postgres container (default `code_forge-forge-postgres`,
#     the database this project actually uses — NOT the compose service in
#     docker-compose.yml, which is a separate, empty one), and waits until it
#     accepts connections;
#   * sets DATABASE_URL for it — the one process env var elstin reads — from
#     what the container itself says: its published port and its
#     POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB. Nothing about the
#     database is hardcoded here, and the password is never printed. The
#     variable is overwritten, not inherited: a stale DATABASE_URL in your
#     shell must not point a dev server at some other database;
#   * makes sure .venv exists with the dependencies installed, and that the
#     UI bundle under settings-ui/vendor/ is not older than ui-src/src/;
#   * applies migrations (`alembic upgrade head` — on your real data, so a
#     branch with a new migration changes that database when you start it),
#     then runs uvicorn in the foreground.
#
# Ctrl-C stops the server. Postgres is left running; stop it with
# `docker stop <container>` (or `podman stop`).
#
# Everything after the script name goes to uvicorn, e.g. `--log-level debug`.
# `--reload` is a poor fit: the daemon runs inside this process, so a
# reload kills whatever agent run is in flight.
#
# Usage: scripts/start-local.sh [uvicorn args...]
#
# Knobs:
#   ELSTIN_PG_CONTAINER  the Postgres container   (default code_forge-forge-postgres)
#   ELSTIN_APP_PORT      port for the server      (default 8000)
#   ELSTIN_APP_HOST      address the server binds (default 127.0.0.1 — there
#                        is no auth, so not the LAN unless you say so)

set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

PG_CONTAINER="${ELSTIN_PG_CONTAINER:-code_forge-forge-postgres}"
APP_HOST="${ELSTIN_APP_HOST:-127.0.0.1}"
APP_PORT="${ELSTIN_APP_PORT:-8000}"
VENV="$repo/.venv"
PG_READY_TIMEOUT_SECONDS=60

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

port_in_use() {
    command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# Checked first: a taken port is the likeliest failure, and it should
# surface before a dependency install or a UI build, not after.
require_app_port_free() {
    if port_in_use "$APP_PORT"; then
        die "Port $APP_PORT is already in use — another elstin server, or the Docker stack (scripts/stop.sh)? Or run with ELSTIN_APP_PORT=<other port>."
    fi
}

# Docker or Podman, whichever this machine has. Podman is resolved by name
# rather than via the usual `alias docker=podman`: an alias lives in the
# interactive shell and is invisible to a script.
ENGINE=""
resolve_engine() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        ENGINE="docker"
    elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
        ENGINE="podman"
    elif command -v docker >/dev/null 2>&1; then
        die "The Docker daemon is not running. Start Docker Desktop and try again."
    elif command -v podman >/dev/null 2>&1; then
        die "Podman is installed but its machine is not running. Run: podman machine start"
    else
        die "No container engine found. Install Docker Desktop, or Podman."
    fi
}

# Python 3.11+ (pyproject.toml). Tries a few names because macOS's bare
# `python3` is often older than what Homebrew installed alongside it.
ensure_venv() {
    if [ ! -x "$VENV/bin/python" ]; then
        local py="" candidate
        for candidate in python3.12 python3.11 python3.13 python3; do
            if command -v "$candidate" >/dev/null 2>&1 \
                && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
                py="$candidate"
                break
            fi
        done
        [ -n "$py" ] || die "Python 3.11 or newer is required to create .venv, and none was found."
        step "Creating .venv with $py"
        "$py" -m venv "$VENV"
    fi

    # Reinstall when pyproject.toml changed since the last install (a pull
    # that added a dependency), not only when the import fails — the
    # failure would otherwise show up later as a ModuleNotFoundError from
    # some code path, far from its cause.
    local stamp="$VENV/.elstin-pyproject.sha" want
    want="$(shasum pyproject.toml | cut -d' ' -f1)"
    if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ] \
        || ! "$VENV/bin/python" -c 'import elstin, uvicorn, alembic' 2>/dev/null; then
        step "Installing Python dependencies"
        "$VENV/bin/python" -m pip install -q -e ".[dev]"
        printf '%s\n' "$want" > "$stamp"
    fi
}

# The bundle is gitignored build output, so a fresh clone has none and the
# server would answer every page with a shell that has no JS. Rebuild when
# it is missing or older than its sources; skip Node entirely otherwise.
ensure_ui() {
    local bundle="settings-ui/vendor/app.js"
    if [ -f "$bundle" ] \
        && [ -z "$(find ui-src/src ui-src/build.mjs ui-src/package.json -newer "$bundle" -print -quit 2>/dev/null)" ]; then
        return 0
    fi
    if ! command -v npm >/dev/null 2>&1; then
        warn "npm is not installed, so the UI bundle was not built; the API works but the pages will be blank."
        return 0
    fi
    step "Building the UI bundle"
    (
        cd ui-src
        [ -d node_modules ] || npm ci --no-audit --no-fund
        npm run build
    )
}

# --- postgres ----------------------------------------------------------

# One value out of the container's environment (POSTGRES_USER, ...).
container_env() {
    "$ENGINE" container inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PG_CONTAINER" \
        | sed -n "s/^$1=//p" | head -1
}

container_running() {
    [ "$("$ENGINE" container inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null)" = "true" ]
}

# The container's host port is fixed when it is created and cannot be
# remapped, so when something else holds it the only fix is to free it.
# Say which container it is rather than letting the engine fail on the bind.
# This script does not stop it: it is somebody's database.
require_pg_port_free() {
    local port="$1"
    port_in_use "$port" || return 0
    local holder
    holder="$("$ENGINE" ps --format '{{.Names}} {{.Ports}}' | awk -v p=":${port}->" 'index($0, p) { print $1; exit }')"
    if [ -n "$holder" ]; then
        die "Port $port, which $PG_CONTAINER publishes, is held by the running container '$holder'. Stop it ($ENGINE stop $holder) and run this again."
    fi
    die "Port $port, which $PG_CONTAINER publishes, is held by another process. Free it and run this again."
}

start_postgres() {
    "$ENGINE" container inspect "$PG_CONTAINER" >/dev/null 2>&1 \
        || die "No container named '$PG_CONTAINER'. Create it, or set ELSTIN_PG_CONTAINER to the one you use."

    PG_PORT="$("$ENGINE" container inspect -f '{{range (index .HostConfig.PortBindings "5432/tcp")}}{{.HostPort}}{{end}}' "$PG_CONTAINER")"
    PG_USER="$(container_env POSTGRES_USER)"
    PG_PASSWORD="$(container_env POSTGRES_PASSWORD)"
    PG_DB="$(container_env POSTGRES_DB)"
    [ -n "$PG_PORT" ] || die "$PG_CONTAINER publishes no host port for 5432, so the host cannot reach it."
    [ -n "$PG_USER" ] && [ -n "$PG_PASSWORD" ] && [ -n "$PG_DB" ] \
        || die "$PG_CONTAINER has no POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB in its environment, so the connection string cannot be derived."

    if container_running; then
        step "$PG_CONTAINER is already running"
    else
        require_pg_port_free "$PG_PORT"
        step "Starting $PG_CONTAINER"
        "$ENGINE" start "$PG_CONTAINER" >/dev/null || die "$PG_CONTAINER did not start."
    fi

    # TCP, not the unix socket: a fresh data directory's first boot runs a
    # temporary server that listens on the socket only, so a socket-based
    # pg_isready answers "ready" a moment before the real server replaces it.
    step "Waiting for Postgres to accept connections"
    local deadline=$(( SECONDS + PG_READY_TIMEOUT_SECONDS ))
    until "$ENGINE" exec "$PG_CONTAINER" pg_isready -q -h 127.0.0.1 -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; do
        if ! container_running || [ "$SECONDS" -ge "$deadline" ]; then
            say ""
            "$ENGINE" logs --tail 40 "$PG_CONTAINER" || true
            die "$PG_CONTAINER did not become ready."
        fi
        sleep 1
    done
}

# The password goes through stdin, not argv, so it never shows in `ps`; and
# through quote(), because a password with `@`, `/` or `:` would otherwise
# corrupt the URL rather than fail loudly.
urlquote() {
    printf '%s' "$1" | "$VENV/bin/python" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))'
}

require_app_port_free
resolve_engine
ensure_venv
ensure_ui
start_postgres

# `localhost`, not a container hostname: the server is on the host, and
# Postgres is reached through the published port.
export DATABASE_URL="postgresql+psycopg://${PG_USER}:$(urlquote "$PG_PASSWORD")@localhost:${PG_PORT}/${PG_DB}"

step "alembic upgrade head"
"$VENV/bin/alembic" upgrade head

say ""
say "${GREEN}Starting elstin from this checkout.${RESET}"
say "  Dashboard  http://localhost:${APP_PORT}/"
say "  Settings   http://localhost:${APP_PORT}/settings"
say "  Postgres   ${PG_CONTAINER}  ${DIM}(postgresql+psycopg://${PG_USER}:***@localhost:${PG_PORT}/${PG_DB})${RESET}"
say "${DIM}Ctrl-C stops the server; Postgres keeps running — stop it with: $ENGINE stop $PG_CONTAINER${RESET}"
say ""

# exec: Ctrl-C and SIGTERM reach uvicorn directly, so its shutdown (the
# daemon's included) is not racing a wrapper shell.
exec "$VENV/bin/python" -m uvicorn elstin.app:create_app --factory \
    --host "$APP_HOST" --port "$APP_PORT" "$@"
