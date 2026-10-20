# Packaging the Mac app

The macOS app is two things in one `.app`: a small native window (Phase 3) and the
**backend** — this directory builds the backend as a self-contained program that runs
with no Python, no virtualenv and nothing else installed.

```bash
packaging/build-backend.sh        # -> build/dist/elstin-backend/elstin-backend
```

Takes a couple of minutes. It needs `uv`, `node`/`npm` and Xcode's command line tools, and
nothing else: the Python it builds with is fetched by `uv` (python-build-standalone) into
`build/python/`, and the whole of `build/` is git-ignored.

## What it does, and the gates it enforces

1. Builds the web UI.
2. **Stages a clean copy of the source** — only files git tracks or would track, so
   `.gitignore` keeps databases, CSV exports, worktrees, `.env` files and your `repos/`
   out of the build — and **fails if anything data-shaped got in anyway**.
3. Builds with a standalone Python 3.12, **not Homebrew's**: Homebrew's libpython targets
   the macOS it was built on (26), so an app frozen from it would not start on anything older.
4. Installs from `requirements.lock` — every package pinned **with hashes**, so a build
   is made from the packages that were tested and one swapped on the index afterwards is
   refused. (`packaging/update-lock.sh` re-resolves them, deliberately, on a branch.)
5. Freezes it with PyInstaller (`elstin-backend.spec`), arm64 only.
6. **Runs the frozen program's own `--self-test`**: every elstin module imports, the
   libraries it reaches for lazily are there, the UI and migrations are in place, a
   database migrates, LangGraph parks a run and a brand-new connection resumes it, and the
   model client turns "nothing is listening" into elstin's own error.
7. **Inspects every Mach-O file** (`inspect_bundle.py`): arm64, the oldest macOS each needs,
   and that none links to a library outside the bundle or the system (no Homebrew paths).

## The macOS floor is measured: 15 (Sequoia)

`orjson` is imported by `langgraph` at load time (through `langgraph-sdk`, which needs
`orjson >= 3.11.5`), and every `orjson` from 3.11 on ships only a macOS 15 wheel. The
inspector reports the real minimum and fails the build if anything needs more than
`ELSTIN_MAX_MACOS` (default 15.0) — so a future dependency that requires macOS 26 is a red
build, not a surprise on a colleague's Mac. Supporting macOS 14 would mean building
`orjson` from source with Rust.

## Testing the frozen program

The same end-to-end tests that drive `python -m elstin.launcher` drive the bundle:

```bash
ELSTIN_LAUNCHER="$PWD/build/dist/elstin-backend/elstin-backend" pytest tests/test_launcher.py
```

They cover the ready line, the access token and Host check, the second-instance refusal,
shutdown, the orphan watchdog and file permissions — on the real binary.

## The window: `packaging/build-app.sh` -> `build/Elstin.app`

```bash
packaging/build-app.sh                  # backend + window + app
packaging/build-app.sh --skip-backend   # reuse build/dist/elstin-backend
```

The window is a small native Swift program (`packaging/app/`, SwiftPM, **no dependencies**):
an `NSWindow` around a `WKWebView` that shows elstin's own page, and nothing else. The script
runs its unit tests, builds it for arm64 and macOS 15, draws a placeholder icon (generated from
`packaging/app/Tools/make-icon.swift`, so no binary asset is committed), assembles the app, and
signs it **ad hoc** — enough to run on the Mac that built it. Layout:

```
Elstin.app/Contents/MacOS/Elstin              the window
              Resources/backend/elstin-backend  (+ _internal/)   the frozen backend
              Resources/Elstin.icns   Info.plist
```

What it does when you open it:

* reads your **login shell's environment** (`$SHELL -ilc env`) and starts the backend with it. An
  app opened from Finder gets launchd's near-empty environment, where `node`, `flutter`, `mvn` and
  Homebrew's tools are "command not found"; the agents' commands run in what the backend has.
  `DATABASE_URL` is never taken from the shell, so an old export cannot steer the app.
* starts the backend, waits for its `ELSTIN_READY` line, and opens `/?token=...` once;
* asks the same port as last time (`--prefer-port`), so the page keeps its address and with it the
  theme and sidebar state the UI saves in the browser per address;
* polls `GET /api/status`: while anything is running it **keeps the Mac awake** and shows the count
  on the Dock icon; **Quit asks first** if work is in flight ("Keep Running" is the default) and
  says what quitting costs. **Closing the window does not quit** — runs take hours;
* opens JIRA and GitHub links in your browser, never in the window; a second launch hands over
  to the first; the backend ending on its own gets a dialog (Restart / Show Log / Quit), and
  SIGTERM quits cleanly without asking.

For developing it: `Elstin --backend <path> --backend-arg <arg> ... --inspectable`
(`--check-backend` runs the bundled backend's self-test and exits). Example, on a scratch database
and log, leaving your own data alone:

```bash
DATABASE_URL=sqlite:////tmp/elstin-try/elstin.db build/Elstin.app/Contents/MacOS/Elstin \
    --backend-arg --log-file --backend-arg /tmp/elstin-try/elstin.log
```

`swift test --package-path packaging/app --scratch-path build/swift` runs the window's tests: the
backend's ready/error lines, the shell environment, the navigation policy, the quit wording, and
the supervisor against fake backends (a late line, a crash, a backend that ignores SIGTERM, one that
fills an unread pipe). `ELSTIN_BUNDLE_ID` overrides the bundle identifier (`com.elstin.Elstin`).

## What is not here yet

Not signed for distribution, not notarized, and there is no `.dmg`: Phases 4 and 5. `strip` and UPX are
off on purpose, so the signatures Phase 4 adds stay valid.
