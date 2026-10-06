"""LangSmith tracing for a story's run.

Off unless `langsmith_tracing_enabled` is on (globally, or for one project — a
trace carries prompts, ticket text, source code and tool output to a third
party, so a project can decide for itself). Everything is configured at
/settings and stored in Postgres, like the rest of elstin: the LangSmith SDK's
own `LANGSMITH_*` environment variables are never read for tracing, and while
a run executes here an ambient `LANGSMITH_TRACING=true` is switched off rather
than obeyed.

What one story run shows, as one trace in the configured LangSmith project:

* the graph itself — `elstin:<story key>` with a child per node (planner, dev,
  gate, verifier, qa), from a `LangChainTracer` handed to `graph.ainvoke`
  (`traced_run`);
* every model call, nested under the node that made it — the OpenAI client
  in elstin/llm/openai_compat.py is wrapped with `langsmith.wrappers`, which
  records the messages, the reply and the token usage (cache reads and
  reasoning tokens included) and does nothing when tracing is off;
* every tool call the planner, its explorers and dev make, nested under the
  model turn's node (`tool_span`, from elstin/agents/loop.py);
* the one model call made outside the graph, lessons capture, as its own trace
  (`traced_operation`).

Nothing here may fail a run. A settings or client problem logs a warning and
the run goes on untraced; delivery to LangSmith is the SDK's own background
thread, which logs and drops what it cannot send.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, tracing_context
from langsmith.run_helpers import trace
from langsmith.utils import tracing_is_enabled

from elstin.config.store import get_config, resolve_bool_config, resolve_config
from elstin.services.redaction import redact_value

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://api.smith.langchain.com"
DEFAULT_PROJECT = "elstin"

# A tool's arguments and result can be a whole file. LangSmith is for reading a
# run, not for holding every byte of it, so a string longer than this is cut
# and says how much was left out. The model calls are not cut: they are the
# trace, and elstin's own activity feed keeps the full tool output.
MAX_TRACED_CHARS = 4000


@dataclass(frozen=True)
class TracingSettings:
    api_key: str
    project: str
    endpoint: str
    workspace_id: str | None


_warned: set[str] = set()
_clients: dict[tuple, Client] = {}
_clients_lock = threading.Lock()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


def tracing_settings(project_id: int | None) -> TracingSettings | None:
    """The settings tracing runs with for this project, or None when it is off —
    not turned on, no API key, or the settings could not be read."""
    try:
        if not resolve_bool_config("langsmith_tracing_enabled", project_id):
            return None
        api_key = (get_config("langsmith_api_key") or "").strip()
        if not api_key:
            _warn_once("LangSmith tracing is on but no LangSmith API key is set at /settings — not tracing")
            return None
        return TracingSettings(
            api_key=api_key,
            project=(resolve_config("langsmith_project", project_id) or "").strip() or DEFAULT_PROJECT,
            endpoint=((get_config("langsmith_endpoint") or "").strip() or DEFAULT_ENDPOINT).rstrip("/"),
            workspace_id=(get_config("langsmith_workspace_id") or "").strip() or None,
        )
    except Exception:  # noqa: BLE001 — tracing is never a reason for a run to fail
        logger.exception("could not read the LangSmith settings — not tracing")
        return None


def _client(settings: TracingSettings) -> Client:
    """One SDK client per (key, endpoint, workspace): each owns a background
    thread that batches and sends runs, so a client per story would be a
    thread per story. A key edited at /settings simply gets a new client."""
    key = (settings.api_key, settings.endpoint, settings.workspace_id)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            extra = {"workspace_id": settings.workspace_id} if settings.workspace_id else {}
            # The anonymizer runs over every run's inputs and outputs before they
            # are sent — model messages, graph state and tool spans alike — so a
            # credential in tool output never leaves the machine.
            client = Client(api_url=settings.endpoint, api_key=settings.api_key, anonymizer=redact_value, **extra)
            _clients[key] = client
        return client


def _open(project_id: int | None) -> tuple[TracingSettings, Client] | None:
    settings = tracing_settings(project_id)
    if settings is None:
        return None
    try:
        return settings, _client(settings)
    except Exception:  # noqa: BLE001
        logger.exception("could not set up the LangSmith client — not tracing")
        return None


@contextmanager
def traced_run(
    project_id: int | None, *, name: str, metadata: dict[str, Any] | None = None, tags: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Around one `graph.ainvoke`. Yields the keys to merge into that call's
    config — a tracer, the run's name, its metadata and tags — or {} when
    tracing is off. Off also means off for the SDK's own environment switch,
    for as long as the run lasts."""
    opened = _open(project_id)
    if opened is None:
        with tracing_context(enabled=False):
            yield {}
        return
    settings, client = opened
    tracer = LangChainTracer(client=client, project_name=settings.project)
    with tracing_context(enabled=True, client=client, project_name=settings.project):
        yield {"callbacks": [tracer], "run_name": name, "metadata": dict(metadata or {}), "tags": list(tags or [])}


@contextmanager
def traced_operation(
    project_id: int | None, *, name: str, metadata: dict[str, Any] | None = None, tags: list[str] | None = None,
) -> Iterator[None]:
    """A model call made outside the graph, as a trace of its own."""
    opened = _open(project_id)
    if opened is None:
        with tracing_context(enabled=False):
            yield
        return
    settings, client = opened
    with tracing_context(enabled=True, client=client, project_name=settings.project):
        with trace(name, run_type="chain", metadata=dict(metadata or {}), tags=list(tags or [])):
            yield


def _clip(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_TRACED_CHARS:
            return value
        return f"{value[:MAX_TRACED_CHARS]}… [{len(value) - MAX_TRACED_CHARS} more characters not traced]"
    if depth < 3 and isinstance(value, dict):
        return {key: _clip(item, depth + 1) for key, item in value.items()}
    if depth < 3 and isinstance(value, (list, tuple)):
        return [_clip(item, depth + 1) for item in value]
    return value


class ToolSpan:
    """The open span of one tool call, or a stand-in that does nothing."""

    def __init__(self, run: Any = None) -> None:
        self._run = run

    def finish(self, output: str, *, is_error: bool) -> None:
        if self._run is None:
            return
        self._run.add_outputs({"output": _clip(output), "is_error": is_error})
        if is_error:
            self._run.error = _clip(output)


@contextmanager
def tool_span(name: str, arguments: dict[str, Any]) -> Iterator[ToolSpan]:
    """Around one tool call in elstin/agents/loop.py. Costs a boolean when
    tracing is off."""
    if not tracing_is_enabled():
        yield ToolSpan()
        return
    with trace(name, run_type="tool", inputs=_clip(arguments)) as run:
        yield ToolSpan(run)
