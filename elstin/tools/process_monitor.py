"""Run a command and measure the memory its whole process tree used.

elstin runs up to `max_parallel_stories` stories at once, and what each one runs
is a build: `mvn test`, `vue-tsc`, `flutter analyze` — a JVM, a Node process and
whatever those spawn. Three of them at once can exhaust a laptop, and nothing said
which command was the heavy one or how heavy. `run` is `subprocess.run` that also
samples the child's tree (the child and every descendant, summed) while it works
and remembers the peak, so a story's run outcome can say "this run peaked at 3.2 GB
in `mvn test`" and a person can set `max_parallel_stories` from data.

It only measures. Nothing is paused, throttled or killed for using memory — a
ceiling is worth adding once these numbers show where it belongs.

The figure is the sum of each process's resident set size, so a page shared between
processes (a JVM's mapped libraries) is counted once per process: an upper bound on
what the tree adds, and the right one for "will three of these fit". Sampling is
every `SAMPLE_INTERVAL_SECONDS`, so a spike shorter than that can be missed — the
peak is the highest sampled, not the highest reached.

Where a measurement goes: `measuring(run_id)` marks the code running under it (and
the threads it starts) as belonging to a run, and `take_peak(run_id)` collects the
highest figure its commands reached (elstin/services/memory_usage.py persists it).
Outside `measuring`, `run` behaves exactly like `subprocess.run` and records nothing.
A host where psutil cannot read a process measures nothing; the command is unaffected.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:  # a dependency, but a host that lacks it must still run commands
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

SAMPLE_INTERVAL_SECONDS = 0.5
# Long enough to name the command in a build log without storing a whole classpath.
MAX_COMMAND_CHARS = 200


@dataclass(frozen=True)
class Peak:
    """The highest memory one command's process tree reached while it ran."""

    rss_bytes: int
    command: str

    @property
    def rss_mb(self) -> int:
        return round(self.rss_bytes / (1024 * 1024))


_run: ContextVar[str | None] = ContextVar("measured_run", default=None)
_peaks: dict[str, Peak] = {}
_lock = threading.Lock()


@contextmanager
def measuring(run_id: str | None) -> Iterator[None]:
    """Attribute the commands `run` executes in here — and in threads started from
    here — to `run_id`. A no-op for a falsy id."""
    token = _run.set(run_id or None)
    try:
        yield
    finally:
        _run.reset(token)


def take_peak(run_id: str) -> Peak | None:
    """The highest peak any measured command of `run_id` reached since the last
    call, and forget it. None when nothing was measured."""
    with _lock:
        return _peaks.pop(run_id, None)


def _note(run_id: str, peak: Peak) -> None:
    with _lock:
        current = _peaks.get(run_id)
        if current is None or peak.rss_bytes > current.rss_bytes:
            _peaks[run_id] = peak


def tree_rss_bytes(pid: int) -> int:
    """Resident memory of process `pid` and all its descendants, 0 when it cannot
    be read (it exited, or the host denies it)."""
    if psutil is None:
        return 0
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except psutil.Error:
            continue  # exited between listing and reading
    return total


def run(
    cmd: Sequence[str],
    *,
    cwd: str,
    timeout: float | None,
    env: Mapping[str, str] | None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """`subprocess.run(cmd, cwd=, capture_output=True, text=True, check=False,
    timeout=, env=, input=)` — same result, same TimeoutExpired, same kill of the
    child on a timeout — plus a sample of the process tree's memory while it runs.

    `env` has no default on purpose: a command that executes a worktree's code must
    say what environment it gets (elstin/tools/agent_env.py)."""
    run_id = _run.get()
    stop = threading.Event()
    highest = [0]

    def sample(pid: int) -> None:
        while True:
            highest[0] = max(highest[0], tree_rss_bytes(pid))
            if stop.wait(SAMPLE_INTERVAL_SECONDS):
                break
        highest[0] = max(highest[0], tree_rss_bytes(pid))  # one last look as it exits

    with subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        sampler = None
        if run_id and psutil is not None:
            sampler = threading.Thread(target=sample, args=(process.pid,), name="memory-sampler", daemon=True)
            sampler.start()
        try:
            try:
                stdout, stderr = process.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            except BaseException:
                process.kill()
                raise
        finally:
            stop.set()
            if sampler is not None:
                sampler.join(timeout=5)
                if highest[0] > 0:
                    _note(run_id, Peak(highest[0], " ".join(str(part) for part in cmd)[:MAX_COMMAND_CHARS]))
        return subprocess.CompletedProcess(process.args, process.poll(), stdout, stderr)
