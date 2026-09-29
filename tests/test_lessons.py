"""Lesson capture: what is mined, what is refused, and what is stored.

The model is faked at `chat_completion_json` — under test is which stories
are mined at all, what the evidence and the guards do to a reply, and how a
repeated lesson is counted, not the model's prose."""
from __future__ import annotations

import os

import pytest

import forge.services.lessons as lessons
from forge.services.generation import GenerationError
from forge.services.usage import record_run_outcome
from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db")

QUOTE = "javac crashed: unsupported release 26"


@pytest.fixture()
def world():
    sm = StateManager()
    project_id = sm.create_project("p")
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="add search")
    return sm, sm.get_epic("ENG-1"), project_id


def _reply(*items):
    async def fake(**kwargs):
        fake.calls.append(kwargs)
        return {"lessons": list(items)}, {}

    fake.calls = []
    return fake


def _lesson(text="Build with JDK 21; the default JDK 26 crashes javac.", quote=QUOTE, **extra):
    return {"text": text, "kind": "environment", "scope_globs": [], "evidence_quote": quote, **extra}


def _enable(project_id):
    from forge.config.store import set_project_config

    set_project_config(project_id, "capture_lessons", "true")


def _trouble(story_key="ENG-2"):
    record_run_outcome(story_key, gate_runs=2, gate_failures=1)
    from forge.services.run_activity import append_activity

    append_activity(story_key, type="status", phase="verifier", title="Gate failed", detail=QUOTE)


# --- when it runs -------------------------------------------------------


async def test_off_by_default_makes_no_call(world, monkeypatch):
    sm, epic, _ = world
    _trouble()
    fake = _reply(_lesson())
    monkeypatch.setattr(lessons, "chat_completion_json", fake)

    assert await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2")) == []
    assert fake.calls == []


async def test_a_story_with_no_friction_is_not_mined(world, monkeypatch):
    sm, epic, project_id = world
    _enable(project_id)
    record_run_outcome("ENG-2", gate_runs=1, gate_failures=0, verifier_runs=1)
    fake = _reply(_lesson())
    monkeypatch.setattr(lessons, "chat_completion_json", fake)

    assert await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2")) == []
    assert fake.calls == []


async def test_a_run_that_predates_the_counters_is_not_trouble(world, monkeypatch):
    """No outcome row means "not measured", which must not read as friction."""
    sm, epic, project_id = world
    _enable(project_id)
    fake = _reply(_lesson())
    monkeypatch.setattr(lessons, "chat_completion_json", fake)

    assert await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2")) == []
    assert fake.calls == []


# --- what is stored -----------------------------------------------------


async def test_a_quoted_lesson_is_stored_as_proposed(world, monkeypatch):
    sm, epic, project_id = world
    _enable(project_id)
    _trouble()
    monkeypatch.setattr(lessons, "chat_completion_json", _reply(_lesson(scope_globs=["pom.xml"])))

    created = await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2"))

    assert len(created) == 1
    [row] = sm.list_project_lessons(project_id)
    assert row["status"] == "proposed"
    assert row["kind"] == "environment"
    assert row["scope_globs"] == ["pom.xml"]
    assert row["evidence_story_keys"] == ["ENG-2"]
    assert row["evidence_quote"] == QUOTE


async def test_a_lesson_whose_quote_is_not_in_the_evidence_is_dropped(world, monkeypatch):
    sm, epic, project_id = world
    _enable(project_id)
    _trouble()
    monkeypatch.setattr(lessons, "chat_completion_json", _reply(_lesson(quote="the build always fails on Fridays")))

    assert await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2")) == []
    assert sm.list_project_lessons(project_id) == []


async def test_a_repeated_lesson_is_counted_not_duplicated(world, monkeypatch):
    sm, epic, project_id = world
    _enable(project_id)
    _trouble()
    monkeypatch.setattr(lessons, "chat_completion_json", _reply(_lesson()))
    await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2"))

    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="more")
    _trouble("ENG-3")
    reworded = _lesson(text="build with jdk 21, the default JDK 26 crashes javac")
    monkeypatch.setattr(lessons, "chat_completion_json", _reply(reworded))
    created = await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-3"))

    assert created == []
    [row] = sm.list_project_lessons(project_id)
    assert row["occurrences"] == 2
    assert row["evidence_story_keys"] == ["ENG-2", "ENG-3"]


async def test_a_rejected_lesson_stays_rejected_when_proposed_again(world, monkeypatch):
    sm, epic, project_id = world
    _enable(project_id)
    _trouble()
    monkeypatch.setattr(lessons, "chat_completion_json", _reply(_lesson()))
    await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2"))
    [row] = sm.list_project_lessons(project_id)
    sm.set_lesson_status(row["id"], "rejected")

    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="more")
    _trouble("ENG-3")
    await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-3"))

    assert [r["status"] for r in sm.list_project_lessons(project_id)] == ["rejected"]


async def test_known_lessons_are_shown_to_the_model(world, monkeypatch):
    sm, epic, project_id = world
    _enable(project_id)
    _trouble()
    sm.add_lesson_candidate(project_id, "Never edit orders-oas.yaml by hand.")
    fake = _reply()
    monkeypatch.setattr(lessons, "chat_completion_json", fake)

    await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2"))

    assert "Never edit orders-oas.yaml by hand." in fake.calls[0]["messages"][1]["content"]


async def test_a_model_failure_is_swallowed(world, monkeypatch):
    sm, epic, project_id = world
    _enable(project_id)
    _trouble()

    async def boom(**kwargs):
        raise GenerationError("provider down")

    monkeypatch.setattr(lessons, "chat_completion_json", boom)

    assert await lessons.capture_story_lessons(sm, epic, sm.get_story("ENG-2")) == []


# --- validation, evidence, manager --------------------------------------


def test_validation_enforces_size_and_caps_the_count():
    evidence = f"x {QUOTE} y"
    too_short = _lesson(text="Use JDK 21.")
    too_long = _lesson(text="x" * 400)
    good = [_lesson(text=f"Lesson number {i} about the toolchain here.") for i in range(5)]

    kept = lessons.validate_lessons([too_short, too_long, *good], evidence)

    assert len(kept) <= lessons.MAX_LESSONS_PER_STORY
    assert all(lessons.MIN_LESSON_CHARS <= len(k["text"]) <= lessons.MAX_LESSON_CHARS for k in kept)


def test_validation_tolerates_whitespace_and_case_in_the_quote():
    kept = lessons.validate_lessons([_lesson(quote="JAVAC  crashed:\nunsupported release 26")], f"a {QUOTE} b")
    assert len(kept) == 1


def test_evidence_keeps_the_newest_events_when_it_must_cut():
    story = {"jira_key": "ENG-2", "summary": "s"}
    events = [
        {"type": "status", "phase": "dev", "title": f"event {i}", "detail": "d" * 500} for i in range(200)
    ]
    text = lessons.build_evidence(story, None, events)

    assert len(text) <= lessons.MAX_EVIDENCE_CHARS + 200
    assert "event 199" in text and "event 0 " not in text


def test_evidence_leaves_out_tool_and_token_noise():
    events = [
        {"type": "tool", "phase": "dev", "title": "read_file"},
        {"type": "token", "phase": "dev", "title": "Token usage"},
        {"type": "status", "phase": "dev", "title": "kept"},
    ]
    text = lessons.build_evidence({"jira_key": "ENG-2"}, None, events)
    assert "kept" in text and "read_file" not in text and "Token usage" not in text


def test_unknown_lesson_status_is_refused(world):
    sm, _, project_id = world
    row = sm.add_lesson_candidate(project_id, "Some project lesson text.")
    with pytest.raises(ValueError):
        sm.set_lesson_status(row["id"], "maybe")
    assert sm.set_lesson_status(99999, "accepted") is False


def test_project_deletion_takes_its_lessons(world):
    sm, _, _ = world
    project_id = sm.create_project("empty")
    sm.add_lesson_candidate(project_id, "Some project lesson text.")

    counts = sm.delete_project_records(project_id)

    assert counts["lessons"] == 1
    assert sm.list_project_lessons(project_id) == []


def test_migration_produces_the_table_the_model_expects():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from forge.db.models import ProjectLesson

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    command.upgrade(config, "head")

    columns = {c["name"] for c in inspect(engine).get_columns("project_lessons")}
    expected = {c.name for c in ProjectLesson.__table__.columns}
    assert columns == expected, f"migration/model drift: {expected ^ columns}"
