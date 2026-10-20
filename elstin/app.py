"""FastAPI app factory. Single process: serves the settings UI/API and runs
both of the daemon's background asyncio tasks in the same process (the
plan's "single process, no fleet split" design) — one container, one
`docker compose up`.

Two loops, deliberately separate (see elstin/daemon/ticket_sync.py):
elstin/daemon/main.py's `run_forever` drives orchestration on a fast 15s
cycle, while `run_ticket_sync_forever` re-runs each project's saved JIRA
filter on a much slower one. Neither can stall the other.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from elstin.api.access_guard import Access, AccessGuard
from elstin.api.config_routes import router as config_router
from elstin.api.epic_routes import router as epic_router
from elstin.api.inbox_routes import router as inbox_router
from elstin.api.po_routes import router as po_router
from elstin.api.project_routes import router as project_router
from elstin.api.run_routes import router as run_router
from elstin.api.status_routes import router as status_router
from elstin.daemon.main import run_forever
from elstin.daemon.ticket_sync import run_ticket_sync_forever
from elstin.db.engine import close_engine, database_path
from elstin.db.lock import acquire as acquire_instance_lock, release as release_instance_lock
from elstin.db.migrate import upgrade_to_head
from elstin.resources import resource_root

_UI_DIR = resource_root() / "settings-ui"

# The UI's pages and bundles keep fixed names (settings-ui/vendor/app-*.js),
# so without this a browser could run a cached bundle for a while after a
# rebuild — a button that exists on the server but not on screen. "no-cache"
# still caches: the browser asks every load and gets a 304 when nothing changed.
_REVALIDATE = {"Cache-Control": "no-cache"}


class _RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(_REVALIDATE)
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    # One elstin per database: refuse to start beside another, before touching it.
    database = database_path()
    acquire_instance_lock(database)
    try:
        # Before anything reads the database — including the first request.
        await asyncio.to_thread(upgrade_to_head)
        tasks = [asyncio.create_task(run_forever()), asyncio.create_task(run_ticket_sync_forever())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    finally:
        await asyncio.to_thread(close_engine)  # a clean quit leaves one complete file
        release_instance_lock(database)


def create_app(access: Access | None = None) -> FastAPI:
    """`access` is passed by the Mac app's launcher (elstin/launcher.py) and makes the
    server answer only to its own window — see elstin/api/access_guard.py. `uvicorn
    elstin.app:create_app --factory` calls this with nothing: no access control."""
    app = FastAPI(title="elstin", lifespan=_lifespan)
    if access is not None:
        app.add_middleware(AccessGuard, access=access)

    app.include_router(config_router)
    app.include_router(run_router)
    app.include_router(epic_router)
    app.include_router(project_router)
    app.include_router(po_router)
    app.include_router(inbox_router)
    app.include_router(status_router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        # Browsers probe this path regardless of the <link rel="icon">.
        return FileResponse(_UI_DIR / "favicon.svg", media_type="image/svg+xml")

    # The UI is one app (ui-src/src/App.tsx) with its own client-side routes,
    # so every page URL serves the same shell and the bundle picks the view
    # from the path. Only the URLs the app knows are served it — /api,
    # /settings-ui, /healthz and /favicon.ico keep their own routes above and
    # below, and an unknown path is a real 404 rather than an empty shell. The
    # old /projects/<id>/po/... pages are among them; the app redirects them.
    def app_shell() -> FileResponse:
        return FileResponse(_UI_DIR / "app.html", headers=_REVALIDATE)

    for path in ("/", "/projects", "/settings", "/projects/{project_id}", "/projects/{project_id}/{rest:path}"):
        app.add_api_route(path, app_shell, methods=["GET"], include_in_schema=False, response_class=FileResponse)

    app.mount("/settings-ui", _RevalidatedStaticFiles(directory=_UI_DIR), name="settings-ui")

    return app
