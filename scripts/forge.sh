#!/usr/bin/env bash
#
# Run the whole stack in Docker.
#
# `docker compose up` already works and remains the documented path; this
# wrapper exists for the parts compose does not do on its own:
#
#   * it fails fast, in one sentence, on the things that actually go wrong
#     on a fresh machine — a stopped engine, a missing Compose, a port
#     already taken — instead of a wall of daemon-socket output, and it
#     finds Podman as readily as Docker (the usual `alias docker=podman`
#     is invisible to a script, so a Podman user would otherwise be told
#     to install an engine they already have);
#   * it waits for /healthz rather than for the container to exist, so
#     "started" means the app is actually serving, and it gives up early
#     (with the app's own logs) when the container dies during boot;
#   * it prints where to go next, which for this app is not the root page
#     but /settings: nothing is read from a .env file, so an unconfigured
#     instance has no JIRA, GitHub or LLM credentials and the daemon has
#     nothing to poll for.
#
# Everything here is a thin shell over compose. Nothing in forge/ depends
# on it, and deleting it costs only the conveniences above.
#
# Usage: scripts/forge.sh [command]
#   up        build, start, wait for health (default)
#   stop      stop the containers, keep them for a fast restart
#   down      stop and remove the containers, keep all data
#   restart   down + up
#   rebuild   rebuild from scratch, ignoring the layer cache, then up
#   logs      follow the app's logs
#   status    what is running, and whether the app is healthy
#   migrate   run `alembic upgrade head` against the running stack
#   shell     a shell inside the app container
#   psql      a psql prompt on the app's database
#   reset     DESTRUCTIVE — delete the database volume and start fresh
#
# Ports come from the same variables compose reads, so they stay in sync:
#   FORGE_APP_PORT=9000 FORGE_PG_PORT=6000 scripts/forge.sh up

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

APP_PORT="${FORGE_APP_PORT:-8000}"
PG_PORT="${FORGE_PG_PORT:-5433}"
# The app answers /healthz almost immediately once uvicorn is up; the
# budget is really for the first run, where compose is still pulling
# postgres and applying migrations before the app is even started.
HEALTH_TIMEOUT_SECONDS="${FORGE_HEALTH_TIMEOUT:-180}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------

# Docker or Podman, whichever this machine actually has. Podman is worth
# resolving explicitly rather than relying on the usual `alias docker=podman`:
# an alias lives in the interactive shell and is invisible to a script,
# so a Podman user running this would otherwise be told to install Docker
# while a working engine sat on their PATH.
ENGINE=""
COMPOSE=()

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

    # Compose v2 as a subcommand of the engine is the common shape;
    # Podman shells out to a compose provider from the same subcommand.
    # The standalone binary is the fallback for older installs.
    if "$ENGINE" compose version >/dev/null 2>&1; then
        COMPOSE=("$ENGINE" compose)
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE=(docker-compose)
    else
        die "$ENGINE is running but Compose is not installed. Install the compose plugin (or docker-compose)."
    fi
}

require_engine() {
    [ -n "$ENGINE" ] || resolve_engine
}

# A port collision surfaces from compose as a bind failure after it has
# already started postgres, which is a confusing place to read it from.
require_port_free() {
    local port="$1" what="$2" var="$3"
    # Only complain about a port held by something that is not our own
    # stack — re-running `up` on an already-running instance is fine.
    if [ -n "$("${COMPOSE[@]}" ps -q 2>/dev/null)" ]; then
        return 0
    fi
    if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        die "Port $port ($what) is already in use. Free it, or run with $var=<other port>."
    fi
}

# --- health ------------------------------------------------------------

app_container() { "${COMPOSE[@]}" ps -q app 2>/dev/null | head -1; }

app_is_healthy() {
    curl -fsS --max-time 3 "http://localhost:${APP_PORT}/healthz" >/dev/null 2>&1
}

wait_for_health() {
    local deadline=$(( SECONDS + HEALTH_TIMEOUT_SECONDS ))
    step "Waiting for the app to answer on port ${APP_PORT}"
    while [ "$SECONDS" -lt "$deadline" ]; do
        if app_is_healthy; then
            return 0
        fi
        # Bail out the moment the container is gone rather than sitting
        # out the full timeout on a stack that has already crashed.
        local cid; cid="$(app_container)"
        if [ -z "$cid" ] || [ "$("$ENGINE" inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" != "true" ]; then
            say ""
            say "${RED}The app container stopped while starting. Its last output:${RESET}"
            "${COMPOSE[@]}" logs --tail 40 app || true
            die "Stack did not come up."
        fi
        sleep 2
    done
    say ""
    say "${RED}No response from /healthz after ${HEALTH_TIMEOUT_SECONDS}s. Recent output:${RESET}"
    "${COMPOSE[@]}" logs --tail 40 app || true
    die "Stack did not become healthy."
}

banner() {
    say ""
    say "${GREEN}code_forge is up.${RESET}"
    say "  Dashboard  http://localhost:${APP_PORT}/"
    say "  Settings   http://localhost:${APP_PORT}/settings"
    say "  Postgres   localhost:${PG_PORT}  ${DIM}(forge/forge, db forge)${RESET}"
    say ""
    say "${DIM}Nothing is read from a .env file: open /settings and fill in JIRA,${RESET}"
    say "${DIM}GitHub and your LLM provider before the daemon has work to poll for.${RESET}"
    say "${DIM}Logs: scripts/forge.sh logs   Stop: scripts/stop.sh${RESET}"
}

# --- commands ----------------------------------------------------------

# The image bakes in forge/ and the UI bundle, so an image built before
# your last edit runs code you no longer have — and, since it also carries
# alembic/, a stale image against a current database fails migration with
# "Can't locate revision", which reads like database corruption rather
# than what it is. Building every time removes that whole class of
# confusion; with layers cached an unchanged tree costs a few seconds.
cmd_up() {
    require_engine
    require_port_free "$APP_PORT" "the app" FORGE_APP_PORT
    require_port_free "$PG_PORT" "Postgres" FORGE_PG_PORT
    step "Building the image and starting Postgres, migrations and the app"
    # `migrate` is a one-shot the app waits on, so once this returns the
    # schema is already at head.
    if ! "${COMPOSE[@]}" up -d --build "$@"; then
        explain_startup_failure
        die "Stack did not start."
    fi
    wait_for_health
    banner
}

# Compose reports a failed dependency by name and swallows its output, so
# the one thing you need — what the migration or the app actually said —
# is not on screen. Put it there, and only the relevant half of it: a
# failed migration means the app never started, and dumping the previous
# container's healthy log underneath the real error only obscures it.
exit_code_of() {
    local cid; cid="$("${COMPOSE[@]}" ps -aq "$1" 2>/dev/null | head -1)"
    [ -n "$cid" ] || return 1
    "$ENGINE" inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null
}

explain_startup_failure() {
    local migrate_exit; migrate_exit="$(exit_code_of migrate || echo "")"
    if [ -n "$migrate_exit" ] && [ "$migrate_exit" != "0" ]; then
        local migrate_log
        migrate_log="$("${COMPOSE[@]}" logs --tail 30 migrate 2>/dev/null || true)"
        say ""
        say "${RED}Migrations did not complete (exit $migrate_exit):${RESET}"
        say "$migrate_log"
        case "$migrate_log" in
            *"Can't locate revision"*)
                say ""
                say "That revision is in the database but not in this image or checkout."
                say "The database is newer than the code — pull, or ${BOLD}scripts/forge.sh reset${RESET} to start clean."
                ;;
        esac
        return 0
    fi

    local app_log
    app_log="$("${COMPOSE[@]}" logs --tail 30 app 2>/dev/null || true)"
    if [ -n "$app_log" ]; then
        say ""
        say "${RED}App output:${RESET}"
        say "$app_log"
    fi
}

# Two ways to stop, because they are genuinely different: `stop` parks the
# containers where they are (the next `up` restarts them in place), while
# `down` removes them and the network. Neither touches a volume, so the
# database, worktrees and cloned repos survive both — only `reset` deletes
# data, and it asks first.
cmd_stop() {
    require_engine
    step "Stopping the containers (they are kept, along with all data)"
    "${COMPOSE[@]}" stop
    say "Start again with: ${BOLD}scripts/forge.sh up${RESET}"
}

cmd_down() {
    require_engine
    step "Stopping and removing the containers (volumes, and so all data, are kept)"
    "${COMPOSE[@]}" down
    say "Start again with: ${BOLD}scripts/forge.sh up${RESET}"
}

# `up` already builds. This is the bigger hammer for when the layer cache
# is the problem itself — a changed base image, or a dependency resolved
# from a cached npm/pip layer.
cmd_rebuild() {
    require_engine
    step "Rebuilding the image from scratch (no layer cache)"
    "${COMPOSE[@]}" build --no-cache
    cmd_up
}

cmd_logs() {
    require_engine
    "${COMPOSE[@]}" logs -f --tail 100 "${@:-app}"
}

cmd_status() {
    require_engine
    "${COMPOSE[@]}" ps
    say ""
    # Deliberately gated on the container: a /healthz probe alone would
    # report "healthy" for a uvicorn the developer is running on the same
    # port outside Docker, which is exactly the confusion to avoid.
    if [ -z "$(app_container)" ]; then
        say "The app container is not running. Start it with: ${BOLD}scripts/forge.sh up${RESET}"
    elif app_is_healthy; then
        say "${GREEN}Healthy${RESET} — http://localhost:${APP_PORT}/"
    else
        say "${RED}Running but not answering${RESET} on http://localhost:${APP_PORT}/healthz — see: scripts/forge.sh logs"
    fi
}

cmd_migrate() {
    require_engine
    step "alembic upgrade head"
    "${COMPOSE[@]}" run --rm migrate
}

cmd_shell() {
    require_engine
    "${COMPOSE[@]}" exec app /bin/sh
}

cmd_psql() {
    require_engine
    "${COMPOSE[@]}" exec postgres psql -U forge -d forge
}

cmd_reset() {
    require_engine
    say "${RED}This deletes the Postgres volume: every project, epic, story," \
        "run and setting is lost.${RESET}"
    say "The /data volumes go too: worktrees, cloned repos, JIRA attachments,"
    say "and the dev loop's pre-edit baselines."
    printf 'Type "reset" to confirm: '
    local answer; read -r answer
    [ "$answer" = "reset" ] || die "Cancelled."
    "${COMPOSE[@]}" down -v
    cmd_up
}

# The usage block above is the only help text there is, so print it
# from the file rather than keeping a second copy in sync by hand.
usage() { awk '/^# Usage:/,/^$/ {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"; }

command="${1:-up}"
[ $# -gt 0 ] && shift || true

case "$command" in
    up)       cmd_up "$@" ;;
    stop)     cmd_stop ;;
    down)     cmd_down ;;
    restart)  cmd_down; cmd_up "$@" ;;
    rebuild)  cmd_rebuild ;;
    logs)     cmd_logs "$@" ;;
    status)   cmd_status ;;
    migrate)  cmd_migrate ;;
    shell)    cmd_shell ;;
    psql)     cmd_psql ;;
    reset)    cmd_reset ;;
    -h|--help|help) usage ;;
    *)        die "Unknown command '$command'. Try: scripts/forge.sh --help" ;;
esac
