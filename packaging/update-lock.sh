#!/usr/bin/env bash
#
# Re-resolve the dependencies the Mac app is built from, with hashes.
#
#   packaging/update-lock.sh
#
# Writes packaging/requirements.lock (what pyproject.toml's `dependencies` resolve to
# for Python 3.12 on Apple Silicon) and packaging/requirements-build.lock (PyInstaller
# and what it needs). build-backend.sh installs from them, and `uv pip install`
# refuses any file whose hash differs — so a build is made from the packages that were
# tested, and a package swapped on the index afterwards is not silently shipped.
# Run it deliberately, on a branch, when upgrading; then build, test, commit the lock.

set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
common=(--quiet --generate-hashes --python-version 3.12 --python-platform aarch64-apple-darwin --no-header)
uv pip compile pyproject.toml "${common[@]}" -o packaging/requirements.lock
uv pip compile packaging/requirements-build.txt "${common[@]}" -o packaging/requirements-build.lock
wc -l packaging/requirements.lock packaging/requirements-build.lock
