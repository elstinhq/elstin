#!/usr/bin/env bash
#
# Run elstin on the host, from your own checkout.
#
# `scripts/elstin.sh` runs the whole stack in Docker, where the app is a
# baked image. This is the other mode — the one for working on elstin's
# code — and it does the setup CLAUDE.md lists by hand:
#
#   * makes sure .venv exists with the dependencies installed, and that the
#     UI bundle under settings-ui/vendor/ is not older than ui-src/src/;
#   * runs uvicorn in the foreground. The database is one SQLite file that
#     the server creates and migrates itself on startup (a branch with a new
#     migration changes your real data when you start it); there is nothing
#     else to start.
#
# The file lives in your data directory (~/Library/Application Support/Elstin
# on macOS, ~/.local/share/elstin elsewhere). DATABASE_URL=sqlite:////some/path.db
# puts it somewhere else — to try a branch on a throwaway copy, say.
#
# Ctrl-C stops the server.
#
# Everything after the script name goes to uvicorn, e.g. `--log-level debug`.
# `--reload` is a poor fit: the daemon runs inside this process, so a
# reload kills whatever agent run is in flight.
#
# Usage: scripts/start-local.sh [uvicorn args...]
#
# Knobs:
#   ELSTIN_APP_PORT      port for the server      (default 8000)
#   ELSTIN_APP_HOST      address the server binds (default 127.0.0.1 — there
#                        is no auth, so not the LAN unless you say so)

set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

APP_HOST="${ELSTIN_APP_HOST:-127.0.0.1}"
APP_PORT="${ELSTIN_APP_PORT:-8000}"
VENV="$repo/.venv"

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

require_app_port_free
ensure_venv
ensure_ui

# A DATABASE_URL left over from the Postgres days (CLAUDE.md used to say to
# export one) would be refused by the app; drop it instead, with a word.
case "${DATABASE_URL:-}" in
    sqlite:*|"") ;;
    *) warn "DATABASE_URL points at something other than SQLite, which elstin no longer uses; ignoring it."
       unset DATABASE_URL ;;
esac
DB_URL="$("$VENV/bin/python" -c 'from elstin.db.engine import get_database_url; print(get_database_url())')"

say ""
say "${GREEN}Starting elstin from this checkout.${RESET}"
say "  Dashboard  http://localhost:${APP_PORT}/"
say "  Settings   http://localhost:${APP_PORT}/settings"
say "  Database   ${DB_URL#sqlite:///}"
say "${DIM}Ctrl-C stops the server.${RESET}"
say ""

# exec: Ctrl-C and SIGTERM reach uvicorn directly, so its shutdown (the
# daemon's included) is not racing a wrapper shell.
exec "$VENV/bin/python" -m uvicorn elstin.app:create_app --factory \
    --host "$APP_HOST" --port "$APP_PORT" "$@"
