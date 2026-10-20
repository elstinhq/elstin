"""In-process fan-out of run activity to live (SSE) subscribers.

The activity a human watches during a run is already being written to
the database by elstin/services/run_activity.py's append_activity — every agent
role calls it for each reasoning block, tool call and status change. Until
now the only way to see those events was to re-fetch the whole list on a
timer, which put the UI a poll interval behind the agent and re-read the
run's entire history every few seconds to discover one new row.

This module is the push half of that: append_activity hands each event
here as it is written, and `publish` drops it into the queue of every
listener currently watching that run_id. Because the API and the daemon
share one process by design (see elstin/app.py's lifespan — the poll loop
is an asyncio task next to the routes), an in-memory registry reaches
every writer there is; no Redis, nothing else to run.

Two properties this deliberately does NOT give up by going in-memory:

- **No gap on subscribe.** `iter_events` registers its queue *before*
  reading the backlog out of the database, so an event written in between is
  queued rather than lost, and is then dropped as a duplicate by id. The
  other order — read, then subscribe — has a hole exactly the width of the
  backlog query.
- **No silent loss on a slow reader.** Queues are bounded; a subscriber
  that can't keep up is flagged rather than blocked, and re-reads what it
  missed from the table (the durable record) on its next wakeup. The table
  stays the source of truth and this is only a low-latency shortcut to it.

`publish` is called from both the event loop thread (the async SDK runner
loops in elstin/agents/roles/*.py) and from worker threads (verifier tool
hooks reached via asyncio.to_thread — see elstin/tools/dart_tools.py), so
every subscriber records the loop it was created on and off-loop writers
hand the queue work back through call_soon_threadsafe.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger("elstin.activity")

# Per-subscriber buffer. Generous relative to real event rates (a busy dev
# turn emits single-digit events per second) — reaching this means the
# browser stopped reading, and the DB catch-up path handles it.
_QUEUE_MAXSIZE = 512

# How long a stream may sit silent before it emits a heartbeat. Serves two
# purposes: it keeps an idle connection alive through anything that reaps
# quiet sockets, and it forces a periodic write, which is what makes a
# vanished client raise instead of hanging in the queue forever.
HEARTBEAT_SECONDS = 15.0


class _Subscriber:
    __slots__ = ("run_id", "loop", "queue", "dropped")

    def __init__(self, run_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.run_id = run_id
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        # Set when an event could not be queued; the consumer treats it as
        # "you are behind, re-read from the table" rather than as an error.
        self.dropped = False


_subscribers: dict[str, set[_Subscriber]] = {}
_registry_lock = threading.Lock()


def _register(sub: _Subscriber) -> None:
    with _registry_lock:
        _subscribers.setdefault(sub.run_id, set()).add(sub)


def _unregister(sub: _Subscriber) -> None:
    with _registry_lock:
        peers = _subscribers.get(sub.run_id)
        if peers is None:
            return
        peers.discard(sub)
        if not peers:
            del _subscribers[sub.run_id]


def subscriber_count(run_id: str) -> int:
    """How many live listeners a run has — used by tests, and worth having
    for a "is anyone watching" check later."""
    with _registry_lock:
        return len(_subscribers.get(run_id, ()))


def _offer(sub: _Subscriber, event: dict[str, Any]) -> None:
    """Queue one event for one subscriber, from that subscriber's own loop."""
    try:
        sub.queue.put_nowait(event)
    except asyncio.QueueFull:
        sub.dropped = True


def publish(event: dict[str, Any]) -> None:
    """Fan one just-written activity event out to that run's listeners.

    Never raises: a failure to notify a watcher must not fail the agent
    turn that produced the event — the event is already durable in
    the database by the time we get here, so the worst case is that a browser
    sees it a heartbeat later via the catch-up read instead of instantly.
    """
    run_id = event.get("run_id")
    if not run_id:
        return
    with _registry_lock:
        targets = list(_subscribers.get(run_id, ()))
    if not targets:
        return

    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None

    for sub in targets:
        try:
            if sub.loop is current:
                _offer(sub, event)
            else:
                sub.loop.call_soon_threadsafe(_offer, sub, event)
        except RuntimeError:
            # Subscriber's loop is closing/closed — its generator is being
            # torn down anyway and will unregister itself.
            continue
        except Exception:  # pragma: no cover - defensive, see docstring
            logger.exception("failed to publish activity to a subscriber of run_id=%s", run_id)


async def _read_since(run_id: str, since_id: int) -> list[dict[str, Any]]:
    """list_activity is sync SQLAlchemy; this generator runs on the event
    loop, so the read goes to a worker thread rather than stalling every
    other connection the process is serving.

    Imported here rather than at module scope: run_activity publishes into
    this module on every append, so a top-level import either way round is
    a cycle. The write path (publish) is the hot one and stays import-free;
    this read happens once per subscribe."""
    from elstin.services.run_activity import list_activity

    return await asyncio.to_thread(list_activity, run_id, since_id=since_id)


async def iter_events(
    run_id: str,
    *,
    since_id: int = 0,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[dict[str, Any] | None]:
    """Yield this run's activity events, newest last, forever.

    Starts with everything already recorded after `since_id` (so a fresh
    viewer gets the run's history and a reconnecting one resumes exactly
    where it left off — see the route's Last-Event-ID handling), then
    yields each new event as it is written. A `None` is yielded whenever
    `heartbeat` seconds pass with nothing to send; the caller turns that
    into whatever its transport needs to keep the connection warm.

    The stream is unbounded on purpose — a run can go quiet for minutes
    between phases and still have plenty left to say. The consumer decides
    when to stop by closing the generator (for SSE: the client going away).
    """
    sub = _Subscriber(run_id, asyncio.get_running_loop())
    _register(sub)
    try:
        last_id = since_id
        for event in await _read_since(run_id, last_id):
            last_id = max(last_id, int(event.get("id") or 0))
            yield event

        while True:
            try:
                event: dict[str, Any] | None = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
            except (asyncio.TimeoutError, TimeoutError):
                event = None

            if sub.dropped:
                # We overflowed at some point; the table has the truth.
                sub.dropped = False
                for missed in await _read_since(run_id, last_id):
                    last_id = max(last_id, int(missed.get("id") or 0))
                    yield missed

            if event is None:
                yield None
                continue

            event_id = int(event.get("id") or 0)
            if event_id and event_id <= last_id:
                continue  # already delivered by a backlog/catch-up read
            last_id = max(last_id, event_id)
            yield event
    finally:
        _unregister(sub)
