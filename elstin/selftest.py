"""`elstin-app --self-test`: does this build have everything it needs?

A packaged app fails in ways a checkout does not: a module only imported inside a
function that the freezer never saw, a native extension left out, a data file not
copied. Those show up as an ImportError in front of a user, on the one screen that
uses them. This runs, from inside the build and with no network and no credentials,
the things that would fail:

* every module of elstin imports (a missing one is found by name);
* the third-party libraries the code reaches for lazily import, and the CA bundle
  the HTTPS clients need is there;
* the UI and the migrations are where `resources.resource_root()` says;
* a database is created and migrated, and a time written to it comes back aware;
* LangGraph checkpoints a run through a graph, parked at an interrupt, and a brand
  new connection resumes it — the serializer's native code included;
* the model client turns "nothing is listening" into elstin's own error.

Prints one line per check and a last `ELSTIN_SELFTEST {...}` line; exits 0 when all
passed. The build script (packaging/build-backend.sh) runs it against the frozen
binary before anything is signed.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import pkgutil
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# Standalone servers that need `mcp`, which the app itself does not use and the
# build leaves out.
_NOT_PART_OF_THE_APP = ("elstin.mcp",)

# Libraries reached through imports inside functions, or through plugins.
_LIBRARIES = (
    "openai", "httpx", "h11", "anyio", "certifi", "jira", "github", "psutil", "aiosqlite", "sqlalchemy.dialects.sqlite.pysqlite",
    "langgraph.graph", "langgraph.checkpoint.sqlite.aio", "langchain_core", "langsmith", "alembic.ddl.sqlite",
    "pydantic", "fastapi", "starlette", "uvicorn.lifespan.on", "uvicorn.protocols.http.h11_impl", "uvicorn.loops.asyncio",
)


def check_every_elstin_module_imports() -> str:
    import elstin

    names = [
        info.name for info in pkgutil.walk_packages(elstin.__path__, "elstin.")
        if not info.name.startswith(_NOT_PART_OF_THE_APP)
    ]
    assert len(names) > 100, f"only {len(names)} modules found — the package is not being walked ({names[:3]})"
    failed = {}
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — every failure is the finding
            failed[name] = f"{type(exc).__name__}: {exc}"
    assert not failed, f"{len(failed)} of {len(names)} modules fail to import: {failed}"
    return f"{len(names)} modules"


def check_the_libraries_it_reaches_for_are_there() -> str:
    missing = {}
    for name in _LIBRARIES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            missing[name] = f"{type(exc).__name__}: {exc}"
    assert not missing, f"missing: {missing}"

    import certifi
    import httpx

    assert Path(certifi.where()).is_file(), "the CA bundle for HTTPS is not in the build"
    httpx.create_ssl_context()  # builds a TLS context from it
    return f"{len(_LIBRARIES)} libraries, CA bundle present"


def check_the_ui_and_the_migrations_are_where_they_should_be() -> str:
    from elstin.resources import resource_root

    root = resource_root()
    for relative in ("settings-ui/app.html", "settings-ui/favicon.svg", "settings-ui/vendor/app.js", "settings-ui/vendor/app.css", "alembic.ini", "alembic/env.py"):
        assert (root / relative).is_file(), f"{relative} is missing from {root}"
    assert list((root / "alembic" / "versions").glob("*.py")), "no migrations"
    return str(root)


def check_a_database_is_created_migrated_and_keeps_aware_times(scratch: Path) -> str:
    from elstin.db import engine as db
    from elstin.db.migrate import upgrade_to_head
    from elstin.state.manager import StateManager

    upgrade_to_head()
    state = StateManager()  # the one door to the tables; the self-test goes through it like everything else
    project_id = state.create_project("self-test")
    created = state.get_project(project_id)["created_at"]
    assert created.endswith("+00:00"), f"a stored time came back without its zone: {created}"
    assert abs(datetime.fromisoformat(created) - datetime.now(timezone.utc)).total_seconds() < 60
    db.close_engine()
    return "migrated, wrote and read back"


async def _checkpointed_round_trip(url: str) -> list[str]:
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from elstin.orchestration.graph import checkpointer_from_url

    class State(TypedDict):
        log: list[str]
        payload: dict

    def build(checkpointer):
        graph = StateGraph(State)
        graph.add_node("first", lambda state: {"log": state["log"] + ["first"]})
        graph.add_node("second", lambda state: {"log": state["log"] + ["second"]})
        graph.add_edge(START, "first")
        graph.add_edge("first", "second")
        graph.add_edge("second", END)
        return graph.compile(checkpointer=checkpointer, interrupt_after=["first"])

    config = {"configurable": {"thread_id": "selftest"}}
    payload = {"unicode": "naïve ✓", "nested": [1, 2.5, {"k": None}], "when": "2026-10-12T15:30:00+00:00"}
    async with checkpointer_from_url(url) as checkpointer:
        await build(checkpointer).ainvoke({"log": [], "payload": payload}, config)
    # a brand-new connection, as after the app was restarted
    async with checkpointer_from_url(url) as checkpointer:
        graph = build(checkpointer)
        parked = await graph.aget_state(config)
        assert parked.next == ("second",) and parked.values["payload"] == payload, parked
        done = await graph.ainvoke(None, config)
    return done["log"]


def check_langgraph_parks_a_run_and_a_new_connection_resumes_it(scratch: Path) -> str:
    from elstin.db.engine import get_database_url

    log = asyncio.run(_checkpointed_round_trip(get_database_url()))
    assert log == ["first", "second"], log
    return "parked at the interrupt, resumed on a new connection"


def check_the_model_client_reports_an_unreachable_endpoint_as_elstins_own_error() -> str:
    from elstin.llm.errors import LlmUnreachableError
    from elstin.llm.openai_compat import OpenAiCompatProvider
    from elstin.llm.types import LlmRequest, Message, TextBlock

    async def ask() -> None:
        provider = OpenAiCompatProvider(api_key="not-a-key", base_url="http://127.0.0.1:9/v1", connect_timeout_seconds=2)
        await provider.complete(LlmRequest(model="m", system="s", messages=[Message(role="user", content=[TextBlock(text="hi")])]))

    try:
        asyncio.run(ask())
    except LlmUnreachableError as exc:
        return f"{type(exc).__name__}"
    raise AssertionError("expected LlmUnreachableError")


_CHECKS: list[tuple[str, Callable, bool]] = [  # (name, check, needs a scratch database)
    ("every elstin module imports", check_every_elstin_module_imports, False),
    ("the libraries it reaches for are there", check_the_libraries_it_reaches_for_are_there, False),
    ("the UI and the migrations are in place", check_the_ui_and_the_migrations_are_where_they_should_be, False),
    ("a database is created, migrated and keeps aware times", check_a_database_is_created_migrated_and_keeps_aware_times, True),
    ("LangGraph parks a run and a new connection resumes it", check_langgraph_parks_a_run_and_a_new_connection_resumes_it, True),
    ("an unreachable model endpoint is elstin's own error", check_the_model_client_reports_an_unreachable_endpoint_as_elstins_own_error, False),
]


def run() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="elstin-selftest-"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{scratch / 'elstin.db'}"
    failures = 0
    try:
        for name, check, needs_scratch in _CHECKS:
            try:
                detail = check(scratch) if needs_scratch else check()
                print(f"ok    {name} — {detail}", flush=True)
            except Exception:  # noqa: BLE001 — reported, and the rest still run
                failures += 1
                print(f"FAIL  {name}\n{traceback.format_exc()}", flush=True)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"ELSTIN_SELFTEST {json.dumps({'ok': failures == 0, 'checks': len(_CHECKS), 'failed': failures, 'frozen': bool(getattr(sys, 'frozen', False))})}", flush=True)
    return 1 if failures else 0
