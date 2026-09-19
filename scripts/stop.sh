#!/usr/bin/env bash
#
# Stop the stack.
#
# The counterpart to scripts/forge.sh, as its own file because "how do I
# stop this" should not require knowing a subcommand. It is a wrapper, not
# a second implementation: engine detection and everything else live in
# forge.sh, and this delegates so the two can never drift.
#
# Nothing here deletes data. The containers go; the volumes — Postgres,
# worktrees, cloned repos, /data — stay, and `scripts/forge.sh up` brings
# everything back where it was. Deleting data is `scripts/forge.sh reset`,
# which asks first.
#
# Usage:
#   scripts/stop.sh           stop and remove the containers
#   scripts/stop.sh --keep    stop them in place, for a faster restart

set -euo pipefail

forge="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/forge.sh"

case "${1:-}" in
    "")            exec "$forge" down ;;
    --keep|-k)     exec "$forge" stop ;;
    -h|--help)     sed -n '3,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    *)
        printf 'error: unknown option %s. Try: scripts/stop.sh --help\n' "$1" >&2
        exit 1
        ;;
esac
