"""The background sweep that keeps each project's ticket filter fresh.

A second loop rather than work folded into `poll_once`, for two reasons.
The orchestration loop runs every 15s because that is how quickly a
human's approval should turn into a dispatch; a JIRA search per project at
that rate is pointless load against someone else's API for a set of
tickets that changes a few times a day. And the two loops fail
independently — a JIRA outage stalls ingestion without touching stories
that are already mid-run.

Each sweep is `asyncio.to_thread`-ed: `sync_project_filter` is blocking
(HTTP to JIRA, then sync SQLAlchemy through StateManager) and this process
is also serving the settings UI, so running it inline would freeze the API
for the length of a JIRA round-trip per project.

Failures never end the loop. A project whose JIRA credentials are wrong,
or whose filter is malformed, records the error on its filter row (the UI
renders it next to the last-synced time) and the next project is swept as
normal.
"""
from __future__ import annotations

import asyncio
import logging

from forge.config.store import get_config
from forge.services.jira_filter import sync_project_filter
from forge.state.manager import StateManager

logger = logging.getLogger("forge.ticket_sync")

_DEFAULT_INTERVAL_SECONDS = 300
# A floor, not a preference: the setting is a free-text field like every
# other config value, and a fat-fingered "5" would hammer JIRA once per
# five seconds forever.
_MIN_INTERVAL_SECONDS = 30


def poll_interval_seconds() -> int:
    raw = (get_config("jira_filter_poll_seconds") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning("jira_filter_poll_seconds=%r is not a number — using %ds", raw, _DEFAULT_INTERVAL_SECONDS)
        return _DEFAULT_INTERVAL_SECONDS
    return max(value, _MIN_INTERVAL_SECONDS)


async def sync_filters_once(sm: StateManager | None = None) -> None:
    sm = sm or StateManager()
    filters = sm.list_enabled_ticket_filters()
    if not filters:
        return

    for ticket_filter in filters:
        project_id = ticket_filter["project_id"]
        try:
            await asyncio.to_thread(sync_project_filter, project_id)
        except Exception as exc:  # noqa: BLE001 — one project's JIRA problem is not the others'
            # Already recorded on the filter row by sync_project_filter;
            # logged here so an unattended sweep leaves a trace.
            logger.warning("project %s ticket filter sweep failed: %s", project_id, exc)


async def run_ticket_sync_forever() -> None:
    """Re-reads the interval every cycle so changing it at /settings takes
    effect without a restart — the same live-config contract every other
    setting has."""
    logger.info("forge ticket-filter sweep starting, interval=%ss", poll_interval_seconds())
    while True:
        try:
            await sync_filters_once()
        except Exception:
            logger.exception("ticket filter sweep failed")
        await asyncio.sleep(poll_interval_seconds())
