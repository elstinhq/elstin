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


# --- what an agent is shown ---------------------------------------------


def _row(id, text, *, globs=(), occurrences=1):
    return {"id": id, "text": text, "scope_globs": list(globs), "occurrences": occurrences}


def test_the_planner_sees_every_lesson_dev_only_those_its_files_match():
    rows = [
        _row(1, "Build with JDK 21."),
        _row(2, "Run eslint --fix on Vue files.", globs=["src/**/*.vue"]),
        _row(3, "Never hand-edit the OpenAPI copy.", globs=["api/*.yaml"]),
    ]

    assert all(r["text"] in lessons.format_lessons_block(rows) for r in rows)

    dev = lessons.format_lessons_block(rows, ["src/App.vue"])
    assert "Build with JDK 21." in dev
    assert "eslint --fix" in dev  # `**/` matches no directory, so src/App.vue counts
    assert "OpenAPI" not in dev


def test_a_repo_prefix_on_a_planned_path_does_not_hide_a_scoped_lesson():
    rows = [_row(1, "Run eslint --fix on Vue files.", globs=["src/**/*.vue"])]
    assert "eslint" in lessons.format_lessons_block(rows, ["@portal/src/views/Home.vue"])


def test_no_lessons_is_an_empty_block():
    assert lessons.format_lessons_block(None) == ""
    assert lessons.format_lessons_block([], ["a.py"]) == ""


def test_the_budget_drops_what_only_one_story_saw_first():
    common = _row(1, "Confirmed by many stories. " + "x" * 1500, occurrences=5)
    rare = _row(2, "Seen once. " + "y" * 1500, occurrences=1)

    kept = lessons.select_lessons([rare, common])

    assert [r["id"] for r in kept] == [1]


def test_plan_paths_reads_both_file_lists_and_both_entry_shapes():
    plan = {"files_to_create": [{"path": "a.py"}], "files_to_modify": ["b.py", {"path": " c.py "}, {"path": ""}]}
    assert lessons.plan_paths(plan) == ["a.py", "b.py", "c.py"]
    assert lessons.plan_paths(None) == []


def test_both_prompts_carry_lessons_even_without_a_guide():
    from forge.agents.roles.dev_loop import _build_prompt
    from forge.agents.roles.planner_loop import _build_prompt_text

    guide = {"found": False, "lessons": [_row(1, "Build with JDK 21.")]}

    planner = _build_prompt_text(
        user_request="do it", project_path="/p", context_bundle={"project_guide": guide},
        linked_issues_context=None, acceptance_criteria_hint=None, prior_work={}, clarification_answers=None,
    )
    dev = _build_prompt(
        continuing=False, plan={"files_to_modify": ["pom.xml"]}, context_bundle={"project_guide": guide}, brief="b",
    )

    assert "Build with JDK 21." in planner and "PROJECT ARCHITECTURE GUIDE" not in planner
    assert "Build with JDK 21." in dev


def test_a_prompt_without_lessons_is_unchanged():
    from forge.agents.roles.planner_loop import _build_prompt_text

    def prompt(guide):
        return _build_prompt_text(
            user_request="do it", project_path="/p", context_bundle={"project_guide": guide},
            linked_issues_context=None, acceptance_criteria_hint=None, prior_work={}, clarification_answers=None,
        )

    assert prompt({"found": False}) == prompt({"found": False, "lessons": []})
    assert "Lessons learned" not in prompt({"found": False})


# --- reaching the run: only accepted lessons ------------------------------


async def test_a_run_gets_accepted_lessons_and_nothing_else(world):
    from forge.services.project_guide import project_guide_for_run

    sm, _, project_id = world
    accepted = sm.add_lesson_candidate(project_id, "Build with JDK 21 always.")
    sm.set_lesson_status(accepted["id"], "accepted")
    sm.add_lesson_candidate(project_id, "A merely proposed lesson text here.")
    rejected = sm.add_lesson_candidate(project_id, "A rejected lesson text goes here.")
    sm.set_lesson_status(rejected["id"], "rejected")

    guide = await project_guide_for_run(sm, project_id, {})

    assert guide["found"] is False
    assert [row["text"] for row in guide["lessons"]] == ["Build with JDK 21 always."]


async def test_no_accepted_lessons_leaves_the_guide_exactly_as_it_was(world):
    from forge.services.project_guide import project_guide_for_run

    sm, _, project_id = world
    sm.add_lesson_candidate(project_id, "A merely proposed lesson text here.")

    assert await project_guide_for_run(sm, project_id, {}) == {"found": False}


# --- the API ------------------------------------------------------------


@pytest.fixture()
def api(world):
    from fastapi.testclient import TestClient

    from forge.app import create_app

    sm, _, project_id = world
    with TestClient(create_app()) as client:
        yield client, sm, project_id


def test_lessons_are_listed_and_filtered_by_status(api):
    client, sm, project_id = api
    first = sm.add_lesson_candidate(project_id, "Build with JDK 21 always.")
    sm.add_lesson_candidate(project_id, "Never hand-edit the OpenAPI copy.")
    sm.set_lesson_status(first["id"], "accepted")

    assert len(client.get(f"/api/projects/{project_id}/lessons").json()) == 2
    accepted = client.get(f"/api/projects/{project_id}/lessons", params={"status": "accepted"}).json()
    assert [r["text"] for r in accepted] == ["Build with JDK 21 always."]
    assert client.get(f"/api/projects/{project_id}/lessons", params={"status": "maybe"}).status_code == 422
    assert client.get("/api/projects/9999/lessons").status_code == 404


def test_a_lesson_can_be_edited_and_accepted_in_one_call(api):
    client, sm, project_id = api
    row = sm.add_lesson_candidate(project_id, "Build with JDK 21 always.")

    reply = client.put(
        f"/api/projects/{project_id}/lessons/{row['id']}",
        json={"text": "Build with JDK 21; 26 crashes javac.", "status": "accepted"},
    )

    assert reply.status_code == 200
    assert reply.json()["text"] == "Build with JDK 21; 26 crashes javac."
    assert reply.json()["status"] == "accepted"


def test_an_edit_is_held_to_the_same_size_as_a_proposal(api):
    client, sm, project_id = api
    row = sm.add_lesson_candidate(project_id, "Build with JDK 21 always.")
    url = f"/api/projects/{project_id}/lessons/{row['id']}"

    assert client.put(url, json={"text": "x" * (lessons.MAX_LESSON_CHARS + 1)}).status_code == 422
    assert client.put(url, json={"text": "   "}).status_code == 422
    assert client.put(url, json={}).status_code == 422
    assert client.put(url, json={"status": "maybe"}).status_code == 422
    assert sm.get_project_lesson(row["id"])["text"] == "Build with JDK 21 always."


def test_a_lesson_of_another_project_is_not_found(api):
    client, sm, project_id = api
    other = sm.create_project("other")
    row = sm.add_lesson_candidate(other, "Build with JDK 21 always.")

    assert client.put(f"/api/projects/{project_id}/lessons/{row['id']}", json={"status": "accepted"}).status_code == 404
    assert client.delete(f"/api/projects/{project_id}/lessons/{row['id']}").status_code == 404
    assert sm.get_project_lesson(row["id"])["status"] == "proposed"


def test_delete_forgets_a_lesson_so_it_can_be_proposed_again(api):
    client, sm, project_id = api
    row = sm.add_lesson_candidate(project_id, "Build with JDK 21 always.")

    assert client.delete(f"/api/projects/{project_id}/lessons/{row['id']}").json()["deleted"] is True
    assert sm.list_project_lessons(project_id) == []
    assert sm.add_lesson_candidate(project_id, "Build with JDK 21 always.")["created"] is True


# --- the epic's pull request ---------------------------------------------


def _pr_row(id, text, *, status="accepted", stories=("ENG-2",), globs=(), occurrences=1):
    return {
        "id": id, "text": text, "status": status, "evidence_story_keys": list(stories),
        "scope_globs": list(globs), "occurrences": occurrences,
    }


def test_the_pr_section_is_a_paste_ready_block_of_this_epics_accepted_lessons():
    rows = [
        _pr_row(1, "Build with JDK 21; 26 crashes javac.", occurrences=2),
        _pr_row(2, "Run eslint --fix on Vue files.", globs=["src/**/*.vue"]),
    ]

    text = "\n".join(lessons.pr_lessons_section(rows, {"ENG-2", "ENG-3"}))

    assert "## Lessons from building this" in text
    assert "```markdown\n## Learned by code_forge\n- Build with JDK 21; 26 crashes javac.\n" in text
    assert "- Run eslint --fix on Vue files. (applies to `src/**/*.vue`)" in text
    assert text.index("JDK 21") < text.index("eslint")  # most-confirmed first


def test_the_pr_section_leaves_out_other_epics_and_unaccepted_lessons():
    rows = [
        _pr_row(1, "From another epic entirely.", stories=("OTHER-9",)),
        _pr_row(2, "Only proposed, no person vouched yet.", status="proposed"),
        _pr_row(3, "Rejected by a person for good reason.", status="rejected"),
    ]

    assert lessons.pr_lessons_section(rows, {"ENG-2"}) == []
    assert lessons.pr_lessons_section([], {"ENG-2"}) == []


def test_the_pr_section_counts_lessons_still_waiting_for_review_without_showing_them():
    rows = [_pr_row(1, "Build with JDK 21 always."), _pr_row(2, "A guess nobody has reviewed.", status="proposed")]

    text = "\n".join(lessons.pr_lessons_section(rows, {"ENG-2"}))

    assert "1 more from this epic is still waiting for review in forge." in text
    assert "A guess nobody has reviewed." not in text


def test_no_accepted_lessons_means_no_mention_of_waiting_ones():
    assert lessons.pr_lessons_section([_pr_row(1, "Only proposed here.", status="proposed")], {"ENG-2"}) == []


def test_a_lesson_containing_a_code_fence_cannot_break_out_of_the_block():
    text = "\n".join(lessons.pr_lessons_section([_pr_row(1, "Run ```make gen``` before the gate.")], {"ENG-2"}))

    assert text.count("````markdown") == 1  # the outer fence is longer than the one inside
    assert text.rstrip().splitlines()[-1] == "````"
