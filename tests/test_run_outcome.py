"""forge/services/usage.py's run-outcome counters and their Alembic
migration. The counters are the per-run *shape* telemetry
(forge/db/models/run_outcome.py) that any future tuning or eval work has to
read, so what matters here is that they accumulate correctly, that a
missing row stays distinguishable from a row of zeros, and that the table
a deployed database actually gets matches the model.
"""
from __future__ import annotations

import os

import pytest

from forge.services.usage import get_run_outcome, get_story_usage, record_run_outcome

pytestmark = pytest.mark.usefixtures("forge_db")


def test_counters_accumulate_across_calls():
    """Each node records its own slice as the run progresses, so the same
    run_id is written many times and the counters must add up rather than
    overwrite."""
    record_run_outcome("ENG-1", gate_runs=1, gate_failures=1)
    record_run_outcome("ENG-1", gate_runs=1, gate_failures=0)
    record_run_outcome("ENG-1", dev_passes=1, dev_turns=12)
    record_run_outcome("ENG-1", dev_passes=1, dev_turns=5)

    outcome = get_run_outcome("ENG-1")

    assert outcome["gate_runs"] == 2
    assert outcome["gate_failures"] == 1
    assert outcome["dev_passes"] == 2
    assert outcome["dev_turns"] == 17


def test_outcome_and_terminal_reason_overwrite_rather_than_accumulate():
    """The latest value of each describes the run; an earlier soft stop
    that a later pass recovered from should not linger as the answer."""
    record_run_outcome("ENG-1", dev_terminal_reason="no_progress", outcome="verifying")
    record_run_outcome("ENG-1", dev_terminal_reason="end_turn", outcome="completed")

    outcome = get_run_outcome("ENG-1")

    assert outcome["dev_terminal_reason"] == "end_turn"
    assert outcome["outcome"] == "completed"


def test_unrecorded_run_returns_none_not_zeros():
    """A missing row means "not measured", which is a different claim from
    "measured as zero". Collapsing them would make the first real
    measurement look like a regression against a baseline that never
    existed."""
    assert get_run_outcome("ENG-NEVER-RAN") is None


def test_no_run_id_is_a_noop():
    record_run_outcome(None, gate_runs=1)

    assert get_run_outcome("") is None


def test_unknown_counter_is_rejected_loudly():
    """A typo in a counter name would otherwise be silently dropped and the
    column would read zero forever."""
    with pytest.raises(ValueError, match="unknown run-outcome counter"):
        record_run_outcome("ENG-1", gate_failurs=1)


def test_a_write_failure_does_not_propagate(monkeypatch):
    """Telemetry for tuning must never fail a run that is otherwise fine."""
    import forge.services.usage as usage_module

    def boom():
        raise RuntimeError("database gone")

    monkeypatch.setattr(usage_module, "session_scope", boom)

    record_run_outcome("ENG-1", gate_runs=1)  # must not raise


def test_story_usage_carries_the_outcome_and_distinguishes_unmeasured():
    from forge.state.manager import StateManager

    sm = StateManager()
    project_id = sm.create_project("p")
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2", "ENG-3"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="measured")
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="never ran")

    record_run_outcome("ENG-2", gate_runs=2, gate_failures=1, outcome="completed")

    measured = get_story_usage("ENG-2")
    unmeasured = get_story_usage("ENG-3")

    assert measured["run_outcome"]["gate_failures"] == 1
    assert measured["run_outcome"]["outcome"] == "completed"
    assert unmeasured["run_outcome"] is None


def test_migration_produces_the_table_the_model_expects():
    """The test suite builds its schema with create_all, which bypasses
    Alembic entirely — so nothing else here would notice a migration that
    is missing a column the model has. A deployed database only ever gets
    the migration.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from forge.db.models import StoryRunOutcome

    url = os.environ["DATABASE_URL"]
    engine = create_engine(url)
    # forge_db already created every table via create_all; drop them so
    # alembic runs against a clean database, then leave it migrated (the
    # fixture drops everything again on teardown).
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    command.upgrade(config, "head")

    columns = {c["name"] for c in inspect(engine).get_columns("story_run_outcomes")}
    expected = {c.name for c in StoryRunOutcome.__table__.columns}

    assert columns == expected, f"migration/model drift: {expected ^ columns}"
