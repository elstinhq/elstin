"""elstin/services/activity_stream.py + the SSE route that exposes it.

What matters here isn't "does a message arrive" — it's the three ways a
naive push implementation quietly loses events, all of which the UI would
show as an activity feed that has silently stopped updating:

- an event written between "read the history" and "start listening",
- an event published from a verifier tool thread rather than the loop,
- an event dropped because the subscriber's buffer was full.

Each has a test below. Postgres stays the source of truth throughout, so
the assertions are always "the stream agrees with the table".
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("elstin_db")


def _seed_story(project_id: int, story_key: str = "ENG-2") -> str:
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story(story_key, epic_key="ENG-1", wave=0)
    return story_key


def _append(run_id: str, title: str) -> dict:
    from elstin.services.run_activity import append_activity

    return append_activity(run_id, type="status", phase="dev", title=title)


def _is_event(chunk: str) -> bool:
    """An event frame leads with its id line; a heartbeat is a bare comment."""
    return chunk.startswith("id: ")


def _payload(chunk: str) -> dict:
    return json.loads(chunk.split("data: ", 1)[1].strip())


async def _titles(gen, count: int, timeout: float = 5.0) -> list[str]:
    """Pull `count` real events off a stream, skipping heartbeats."""
    seen: list[str] = []
    while len(seen) < count:
        event = await asyncio.wait_for(anext(gen), timeout=timeout)
        if event is not None:
            seen.append(event["title"])
    return seen


async def test_stream_replays_history_then_pushes_new_events(project_id):
    from elstin.services.activity_stream import iter_events

    run_id = _seed_story(project_id)
    _append(run_id, "before subscribe")

    gen = iter_events(run_id, heartbeat=0.05)
    try:
        assert await _titles(gen, 1) == ["before subscribe"]

        _append(run_id, "after subscribe")
        assert await _titles(gen, 1) == ["after subscribe"]
    finally:
        await gen.aclose()


async def test_events_written_during_the_backlog_read_are_not_lost(project_id, monkeypatch):
    """The subscribe-then-read ordering, tested at its seam: an event that
    lands while the history query is in flight must still be delivered, and
    exactly once — this is the gap the reverse ordering would have."""
    import elstin.services.activity_stream as stream_mod

    run_id = _seed_story(project_id)
    _append(run_id, "historical")

    real_read = stream_mod._read_since
    raced = False

    async def racing_read(rid: str, since_id: int):
        nonlocal raced
        rows = await real_read(rid, since_id)
        if not raced:
            raced = True
            _append(rid, "raced in")  # written after the query, before the yield
        return rows

    monkeypatch.setattr(stream_mod, "_read_since", racing_read)

    gen = stream_mod.iter_events(run_id, heartbeat=0.05)
    try:
        assert await _titles(gen, 2) == ["historical", "raced in"]
    finally:
        await gen.aclose()


async def test_since_id_resumes_where_a_dropped_connection_left_off(project_id):
    """What a reconnecting EventSource does with Last-Event-ID."""
    from elstin.services.activity_stream import iter_events

    run_id = _seed_story(project_id)
    first = _append(run_id, "already seen")
    _append(run_id, "missed while disconnected")

    gen = iter_events(run_id, since_id=first["id"], heartbeat=0.05)
    try:
        assert await _titles(gen, 1) == ["missed while disconnected"]
    finally:
        await gen.aclose()


async def test_events_published_from_a_worker_thread_reach_the_stream(project_id):
    """The verifier's tool hooks call append_activity from an
    asyncio.to_thread worker (elstin/tools/dart_tools.py), so publish has to
    cross back into the subscriber's loop rather than assuming it's on it."""
    from elstin.services.activity_stream import iter_events

    run_id = _seed_story(project_id)

    gen = iter_events(run_id, heartbeat=0.05)
    try:
        await asyncio.wait_for(anext(gen), timeout=5.0)  # drain the (empty) backlog heartbeat
        await asyncio.to_thread(_append, run_id, "from a tool thread")
        assert await _titles(gen, 1) == ["from a tool thread"]
    finally:
        await gen.aclose()


async def test_a_subscriber_that_overflows_catches_up_from_postgres(project_id, monkeypatch):
    """A browser that stops reading must not create a hole in the feed: the
    events it couldn't be handed are re-read from the table on the next
    wakeup, in order and without duplicates."""
    import elstin.services.activity_stream as stream_mod

    monkeypatch.setattr(stream_mod, "_QUEUE_MAXSIZE", 2)

    run_id = _seed_story(project_id)
    gen = stream_mod.iter_events(run_id, heartbeat=0.05)
    try:
        await asyncio.wait_for(anext(gen), timeout=5.0)  # subscribed; nothing yet

        for i in range(6):  # three times the buffer
            _append(run_id, f"event {i}")

        assert await _titles(gen, 6) == [f"event {i}" for i in range(6)]
    finally:
        await gen.aclose()


async def test_a_quiet_run_emits_heartbeats(project_id):
    from elstin.services.activity_stream import iter_events

    run_id = _seed_story(project_id)
    gen = iter_events(run_id, heartbeat=0.05)
    try:
        assert await asyncio.wait_for(anext(gen), timeout=5.0) is None
    finally:
        await gen.aclose()


async def test_closing_the_stream_unregisters_the_subscriber(project_id):
    """Otherwise every closed dialog would leave a queue behind that every
    later publish still has to walk."""
    from elstin.services.activity_stream import iter_events, subscriber_count

    run_id = _seed_story(project_id)
    gen = iter_events(run_id, heartbeat=0.05)
    await asyncio.wait_for(anext(gen), timeout=5.0)
    assert subscriber_count(run_id) == 1

    await gen.aclose()
    assert subscriber_count(run_id) == 0


def test_sse_frame_carries_the_event_id_and_json_payload():
    from elstin.api.run_routes import _sse_frame

    frame = _sse_frame({"id": 7, "title": "Planning started", "detail": None})

    assert frame.startswith("id: 7\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1].strip())["title"] == "Planning started"


@pytest.mark.parametrize("raw,expected", [(None, 0), ("", 0), ("12", 12), ("nonsense", 0), ("-4", 0)])
def test_last_event_id_header_is_treated_as_untrusted(raw, expected):
    from elstin.api.run_routes import _parse_last_event_id

    assert _parse_last_event_id(raw) == expected


async def test_route_streams_recorded_activity_as_sse_frames(project_id):
    """The route itself, driven without an HTTP client on purpose: this
    response never ends by design, and TestClient has no way to interrupt an
    open stream — it would block on close rather than fail. Iterating the
    response body directly exercises the same code (backlog -> frames ->
    headers) and lets the test decide when to hang up, which is exactly what
    a real EventSource does."""
    from starlette.requests import Request

    from elstin.api.run_routes import stream_story_activity

    run_id = _seed_story(project_id)
    _append(run_id, "Planning started")
    _append(run_id, "Development started")

    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})
    response = await stream_story_activity(run_id, request)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    body = response.body_iterator
    try:
        chunks: list[str] = []
        while len([c for c in chunks if _is_event(c)]) < 2:
            chunks.append(await asyncio.wait_for(anext(body), timeout=5.0))
    finally:
        await body.aclose()

    assert chunks[0] == "retry: 3000\n\n"  # tells the browser how fast to reconnect
    payloads = [_payload(c) for c in chunks if _is_event(c)]
    assert [p["title"] for p in payloads] == ["Planning started", "Development started"]
    assert chunks[1].startswith(f"id: {payloads[0]['id']}\n")


async def test_a_reconnect_replays_only_what_the_client_missed(project_id):
    from starlette.requests import Request

    from elstin.api.run_routes import stream_story_activity

    run_id = _seed_story(project_id)
    first = _append(run_id, "already seen")
    _append(run_id, "missed while disconnected")

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"last-event-id", str(first["id"]).encode())],
        }
    )
    response = await stream_story_activity(run_id, request)

    body = response.body_iterator
    try:
        chunks: list[str] = []
        while not any(_is_event(c) for c in chunks):
            chunks.append(await asyncio.wait_for(anext(body), timeout=5.0))
    finally:
        await body.aclose()

    payloads = [_payload(c) for c in chunks if _is_event(c)]
    assert [p["title"] for p in payloads] == ["missed while disconnected"]


@pytest.mark.usefixtures("fake_agents")
def test_stream_endpoint_is_wired_up_and_404s_for_an_unknown_story(project_id):
    """Also the one check that the route is mounted where the UI expects
    it — a 404 comes back before any streaming starts, so a plain GET is
    safe here."""
    from elstin.app import create_app

    with TestClient(create_app()) as client:
        response = client.get("/api/stories/NOPE-1/stream")

    assert response.status_code == 404
    assert "NOPE-1" in response.json()["detail"]
