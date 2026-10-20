#!/usr/bin/env bash
#
# Build the Mac app's backend: a self-contained `build/dist/elstin-backend/` that
# runs with no Python, no venv and nothing else installed. (Phase 2 of the DMG; the
# window, signing and the .dmg come after.)
#
#   packaging/build-backend.sh
#
# What it does, in order, stopping at the first thing that is wrong:
#   1. builds the web UI (ui-src -> settings-ui/vendor);
#   2. stages a clean copy of the source: only files git tracks or would track
#      (so .gitignore keeps databases, CSV exports, worktrees and .env files out),
#      and fails if anything that looks like data got in anyway;
#   3. makes a standalone Python 3.12 (python-build-standalone, via uv) — NOT
#      Homebrew's, whose libpython needs macOS 26 — and a venv of its own in build/;
#   4. installs the dependencies from packaging/requirements.lock (so the build is
#      the one that was tested), then elstin itself, then PyInstaller;
#   5. freezes it (packaging/elstin-backend.spec);
#   6. runs the frozen program's own --self-test, and inspects every Mach-O file in
#      the bundle for the macOS it needs and what it links to.
#
# Needs: uv, node + npm, Xcode's command line tools. Output: build/dist/elstin-backend/.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build="$root/build"
stage="$build/stage"
venv="$build/venv"
dist="$build/dist"
# The oldest macOS the app supports, measured: `orjson` (required at import time by
# langgraph, through langgraph-sdk) ships only a macOS 15 wheel from 3.11 on, and
# langgraph-sdk needs >= 3.11.5. Going lower means building orjson from source.
MAX_MACOS="${ELSTIN_MAX_MACOS:-15.0}"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v uv  >/dev/null || die "uv is needed (brew install uv)."
command -v npm >/dev/null || die "node and npm are needed to build the UI."
cd "$root"

say "Building the UI"
(cd ui-src && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run build >/dev/null)
[ -f settings-ui/vendor/app.js ] || die "the UI build produced no settings-ui/vendor/app.js."

say "Staging a clean copy of the source"
rm -rf "$stage" && mkdir -p "$stage"
git ls-files --cached --others --exclude-standard -z -- pyproject.toml elstin alembic alembic.ini settings-ui packaging |
    while IFS= read -r -d '' file; do
        [ -f "$file" ] || continue            # tracked, but deleted in the working tree
        mkdir -p "$stage/$(dirname "$file")"
        cp -p "$file" "$stage/$file"
    done
cp -R settings-ui/vendor "$stage/settings-ui/vendor"   # build output: gitignored, so copied by hand
leaks="$(find "$stage" \( -name '*.db' -o -name '*.db-*' -o -name '*.sqlite*' -o -name '*.csv' -o -name '.env*' -o -name '*.key' -o -name 'repos' -o -name 'node_modules' -o -name '.git' \) -print)"
[ -z "$leaks" ] || die "files that look like data or secrets got into the staged source:
$leaks"

say "Preparing Python 3.12 and the build environment"
export UV_PYTHON_INSTALL_DIR="$build/python"
uv python install 3.12 --quiet
rm -rf "$venv"
uv venv --quiet --python 3.12 "$venv"
if [ -f packaging/requirements.lock ]; then
    uv pip install --quiet --python "$venv/bin/python" -r packaging/requirements.lock
    uv pip install --quiet --python "$venv/bin/python" --no-deps "$stage"
else
    echo "    (no packaging/requirements.lock: installing whatever versions are current)"
    uv pip install --quiet --python "$venv/bin/python" "$stage"
fi
if [ -f packaging/requirements-build.lock ]; then
    uv pip install --quiet --python "$venv/bin/python" -r packaging/requirements-build.lock
else
    uv pip install --quiet --python "$venv/bin/python" -r packaging/requirements-build.txt
fi

say "Freezing"
rm -rf "$dist" "$build/work"
(cd "$stage" && "$venv/bin/pyinstaller" --noconfirm --clean --log-level WARN --distpath "$dist" --workpath "$build/work" packaging/elstin-backend.spec)
backend="$dist/elstin-backend/elstin-backend"
[ -x "$backend" ] || die "PyInstaller did not produce $backend."

say "Self-test, from inside the frozen program"
"$backend" --self-test

say "Inspecting the bundle (macOS floor $MAX_MACOS, arm64, links)"
"$venv/bin/python" packaging/inspect_bundle.py "$dist/elstin-backend" --max-macos "$MAX_MACOS"

say "Done: $(du -sh "$dist/elstin-backend" | cut -f1) in $dist/elstin-backend ($(find "$dist/elstin-backend" -type f | wc -l | tr -d ' ') files)"
