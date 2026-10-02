"""Keeping what a run's commands measured (forge/tools/process_monitor.py).

`measured_run(run_id)` wraps the stretch of a run that executes commands — the
deterministic gate, the dev loop, epic QA's test suite — so that every command inside
it is attributed to the run, and, when it ends, the highest memory any of them reached
is written to the run's outcome row (`story_run_outcomes.peak_memory_mb`, with the
command). Only the maximum is kept: a person setting `max_parallel_stories` needs
"how much can one story's commands take", not a time series.

Never raises, and never changes what the wrapped code does.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from forge.services.usage import record_run_peak_memory
from forge.tools import process_monitor

logger = logging.getLogger(__name__)

T = TypeVar("T")


@contextmanager
def measured_run(run_id: str | None) -> Iterator[None]:
    with process_monitor.measuring(run_id):
        try:
            yield
        finally:
            if run_id:
                try:
                    peak = process_monitor.take_peak(run_id)
                    if peak is not None:
                        record_run_peak_memory(run_id, peak_memory_mb=peak.rss_mb, command=peak.command)
                except Exception:  # noqa: BLE001
                    logger.warning("could not keep the memory measured for %s", run_id, exc_info=True)


async def measured(run_id: str | None, awaitable: Awaitable[T]) -> T:
    """`await awaitable` under `measured_run(run_id)`. The awaitable may be built
    outside: it does nothing until awaited here."""
    with measured_run(run_id):
        return await awaitable
