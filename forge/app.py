"""FastAPI app factory. Single process: serves the settings UI/API and runs
both of the daemon's background asyncio tasks in the same process (the
plan's "single process, no fleet split" design) — one container, one
`docker compose up`.

Two loops, deliberately separate (see forge/daemon/ticket_sync.py):
forge/daemon/main.py's `run_forever` drives orchestration on a fast 15s
cycle, while `run_ticket_sync_forever` re-runs each project's saved JIRA
filter on a much slower one. Neither can stall the other.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from forge.api.config_routes import router as config_router
from forge.api.epic_routes import router as epic_router
from forge.api.project_routes import router as project_router
from forge.api.run_routes import router as run_router
from forge.daemon.main import run_forever
from forge.daemon.ticket_sync import run_ticket_sync_forever

_UI_DIR = Path(__file__).resolve().parent.parent / "settings-ui"

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


def create_app() -> FastAPI:
    app = FastAPI(title="code_forge", lifespan=_lifespan)

    app.include_router(config_router)
    app.include_router(run_router)
    app.include_router(epic_router)
    app.include_router(project_router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/settings")
    def settings_page() -> FileResponse:
        return FileResponse(_UI_DIR / "index.html", headers=_REVALIDATE)

    @app.get("/")
    @app.get("/projects")
    def projects_page() -> FileResponse:
        return FileResponse(_UI_DIR / "dashboard.html", headers=_REVALIDATE)

    @app.get("/projects/{project_id}")
    def project_detail_page(project_id: int) -> FileResponse:
        return FileResponse(_UI_DIR / "project.html", headers=_REVALIDATE)

    app.mount("/settings-ui", _RevalidatedStaticFiles(directory=_UI_DIR), name="settings-ui")

    return app
