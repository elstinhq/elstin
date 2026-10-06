"""The background ticket-filter sweep (elstin/daemon/ticket_sync.py). The
sweep's actual behavior lives in tests/test_jira_filter.py — what matters
here is that the loop picks the right projects, survives one project's
failure, and clamps a bad interval."""
from __future__ import annotations

import pytest

import elstin.daemon.ticket_sync as ticket_sync
from elstin.config.store import set_config
from elstin.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("elstin_db")


def test_poll_interval_defaults_and_clamps():
    assert ticket_sync.poll_interval_seconds() == 300

    set_config("jira_filter_poll_seconds", "600")
    assert ticket_sync.poll_interval_seconds() == 600

    # A too-eager value is floored rather than honoured — this hits someone
    # else's API on a timer.
    set_config("jira_filter_poll_seconds", "5")
    assert ticket_sync.poll_interval_seconds() == 30

    set_config("jira_filter_poll_seconds", "not-a-number")
    assert ticket_sync.poll_interval_seconds() == 300


async def test_sweep_only_touches_enabled_filters(monkeypatch: pytest.MonkeyPatch):
    sm = StateManager()
    on = sm.create_project("On")
    off = sm.create_project("Off")
    sm.set_project_ticket_filter(on, enabled=True, jira_project_key="ENG")
    sm.set_project_ticket_filter(off, enabled=False, jira_project_key="ENG")

    swept: list[int] = []
    monkeypatch.setattr(ticket_sync, "sync_project_filter", lambda project_id: swept.append(project_id))

    await ticket_sync.sync_filters_once()

    assert swept == [on]


async def test_one_projects_failure_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch):
    sm = StateManager()
    first = sm.create_project("First")
    second = sm.create_project("Second")
    sm.set_project_ticket_filter(first, enabled=True, jira_project_key="ENG")
    sm.set_project_ticket_filter(second, enabled=True, jira_project_key="ENG")

    swept: list[int] = []

    def _sweep(project_id: int) -> None:
        swept.append(project_id)
        if project_id == first:
            raise RuntimeError("JIRA is down")

    monkeypatch.setattr(ticket_sync, "sync_project_filter", _sweep)

    await ticket_sync.sync_filters_once()

    assert swept == [first, second]
