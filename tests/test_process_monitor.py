"""elstin/tools/process_monitor.py and what keeps its measurements — how much
memory the commands a run executes take, so `max_parallel_stories` can be set from
data. Real child processes that really allocate."""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import subprocess
import sys
import threading
import time

import pytest

import elstin.orchestration.graph as graph_module
from elstin.services.memory_usage import measured, measured_run
from elstin.services.usage import get_run_outcome, record_run_outcome, record_run_peak_memory
from elstin.tools import language_hooks, process_monitor, safe_shell
from elstin.tools.process_monitor import measuring, run, take_peak, tree_rss_bytes

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _quick_sampling(monkeypatch):
    monkeypatch.setattr(process_monitor, "SAMPLE_INTERVAL_SECONDS", 0.05)
    process_monitor._peaks.clear()
    yield
    process_monitor._peaks.clear()


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# Allocates and touches `mb` megabytes, holds them for `hold` seconds.
def _hog(mb: int, hold: float = 0.6) -> str:
    return (
        f"import time; b = bytearray({mb} * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); "
        f"time.sleep({hold}); print('done')"
    )


def _env() -> dict[str, str]:
    return dict(os.environ)


# --- it is subprocess.run -----------------------------------------------------------


def test_it_returns_what_subprocess_run_returns(tmp_path):
    result = run(_python("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), cwd=str(tmp_path), timeout=20, env=_env())
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")
    assert isinstance(result, subprocess.CompletedProcess) and isinstance(result.stdout, str)


def test_cwd_env_and_input_are_honoured(tmp_path):
    code = "import os, sys; print(os.getcwd(), os.environ.get('MARK'), sys.stdin.read())"
    result = run(_python(code), cwd=str(tmp_path), timeout=20, env={**_env(), "MARK": "yes"}, input="typed")
    cwd, mark, typed = result.stdout.split()
    assert os.path.samefile(cwd, tmp_path) and (mark, typed) == ("yes", "typed")


def test_a_timeout_raises_the_same_way_keeps_the_partial_output_and_kills_the_child(tmp_path):
    marker = tmp_path / "survived"
    code = f"import time; print('partial', flush=True); time.sleep(3); open({str(marker)!r}, 'w').write('x')"
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run(_python(code), cwd=str(tmp_path), timeout=0.5, env=_env())

    assert time.monotonic() - started < 3
    assert b"partial" in caught.value.stdout or "partial" in caught.value.stdout
    time.sleep(3.2)
    assert not marker.exists()  # it did not carry on in the background


def test_a_command_that_cannot_start_raises_as_before(tmp_path):
    with pytest.raises(FileNotFoundError):
        run(["definitely-not-a-command-elstin"], cwd=str(tmp_path), timeout=5, env=_env())


def test_it_needs_to_be_told_the_environment():
    with pytest.raises(TypeError):
        run(["true"], cwd=".", timeout=5)  # type: ignore[call-arg]


# --- measuring -----------------------------------------------------------------------


def test_outside_measuring_nothing_is_recorded_and_no_sampler_runs(tmp_path):
    before = threading.active_count()
    run(_python(_hog(100, hold=0.3)), cwd=str(tmp_path), timeout=20, env=_env())
    assert process_monitor._peaks == {}
    assert threading.active_count() == before


def test_the_peak_includes_what_the_command_allocates(tmp_path):
    with measuring("RUN-1"):
        run(_python(_hog(150)), cwd=str(tmp_path), timeout=30, env=_env())

    peak = take_peak("RUN-1")
    assert peak is not None and peak.rss_mb >= 140
    assert peak.command.startswith(sys.executable)


def test_the_peak_includes_the_processes_it_spawns(tmp_path):
    """A build is a tool that spawns the thing that does the work."""
    child = _hog(150, hold=0.8)
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"

    with measuring("RUN-1"):
        run(_python(parent), cwd=str(tmp_path), timeout=30, env=_env())

    assert take_peak("RUN-1").rss_mb >= 140


def test_the_highest_of_several_commands_is_kept_and_collecting_forgets_it(tmp_path):
    with measuring("RUN-1"):
        run(_python(_hog(200)), cwd=str(tmp_path), timeout=30, env=_env())
        run(_python(_hog(20, hold=0.3)), cwd=str(tmp_path), timeout=30, env=_env())

    assert take_peak("RUN-1").rss_mb >= 190
    assert take_peak("RUN-1") is None


def test_runs_are_measured_apart_even_on_threads(tmp_path):
    def one(run_id: str, mb: int):
        with measuring(run_id):
            run(_python(_hog(mb)), cwd=str(tmp_path), timeout=30, env=_env())

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda args: one(*args), [("SMALL", 20), ("BIG", 220)]))

    small, big = take_peak("SMALL"), take_peak("BIG")
    assert big.rss_mb >= 200 and small.rss_mb < 150


def test_a_long_command_line_is_cut(tmp_path):
    with measuring("RUN-1"):
        run(_python("pass  # " + "x" * 500), cwd=str(tmp_path), timeout=20, env=_env())
    assert len(take_peak("RUN-1").command) == process_monitor.MAX_COMMAND_CHARS


def test_a_host_without_psutil_still_runs_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(process_monitor, "psutil", None)
    with measuring("RUN-1"):
        result = run(_python("print('ran')"), cwd=str(tmp_path), timeout=20, env=_env())
    assert result.stdout.strip() == "ran" and take_peak("RUN-1") is None
    assert tree_rss_bytes(os.getpid()) == 0


def test_a_process_that_is_gone_reads_as_zero():
    assert tree_rss_bytes(2**22 + 12345) == 0


def test_the_trees_own_process_has_resident_memory():
    assert tree_rss_bytes(os.getpid()) > 10 * MB


# --- the shared runners ---------------------------------------------------------------


@pytest.mark.usefixtures("elstin_db")
def test_the_gates_runner_and_the_dev_shell_are_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(safe_shell, "is_safe_command", lambda command: True)
    script = tmp_path / "heavy.sh"
    script.write_text(f"#!/bin/sh\n{sys.executable} -c \"{_hog(120, hold=0.5)}\"\n")
    script.chmod(0o755)

    with measuring("GATE"):
        language_hooks.run([str(script)], cwd=str(tmp_path), timeout=30)
    with measuring("SHELL"):
        safe_shell.run_safe_command(str(tmp_path), str(script))

    assert take_peak("GATE").rss_mb >= 110 and take_peak("SHELL").rss_mb >= 110


# --- keeping it -------------------------------------------------------------------------


@pytest.mark.usefixtures("elstin_db")
def test_the_peak_is_a_maximum_and_none_means_not_measured():
    assert get_run_outcome("ENG-2") is None
    record_run_outcome("ENG-2", gate_runs=1)
    unmeasured = get_run_outcome("ENG-2")
    assert unmeasured["peak_memory_mb"] is None and unmeasured["peak_memory_command"] is None  # not zero

    record_run_peak_memory("ENG-2", peak_memory_mb=900, command="mvn test")
    record_run_peak_memory("ENG-2", peak_memory_mb=300, command="npm run lint")  # smaller: ignored
    record_run_peak_memory("ENG-2", peak_memory_mb=2200, command="mvn verify")

    kept = get_run_outcome("ENG-2")
    assert (kept["peak_memory_mb"], kept["peak_memory_command"]) == (2200, "mvn verify")
    assert kept["gate_runs"] == 1  # the counters were not disturbed


@pytest.mark.usefixtures("elstin_db")
def test_nothing_is_recorded_for_no_run_or_no_measurement():
    record_run_peak_memory(None, peak_memory_mb=500, command="x")
    record_run_peak_memory("ENG-9", peak_memory_mb=0, command="x")
    assert get_run_outcome("ENG-9") is None


@pytest.mark.usefixtures("elstin_db")
def test_a_failure_to_record_never_propagates(monkeypatch):
    import elstin.services.usage as usage

    def broken():
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(usage, "session_scope", broken)
    record_run_peak_memory("ENG-2", peak_memory_mb=500, command="x")  # no exception


@pytest.mark.usefixtures("elstin_db")
def test_a_measured_run_keeps_its_peak_on_exit_even_when_the_work_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with measured_run("ENG-3"):
            run(_python(_hog(150)), cwd=str(tmp_path), timeout=30, env=_env())
            raise RuntimeError("the gate crashed")
    assert get_run_outcome("ENG-3")["peak_memory_mb"] >= 140


@pytest.mark.usefixtures("elstin_db")
async def test_the_measurement_follows_the_work_into_a_thread(tmp_path):
    """The gate runs its commands off the event loop (`asyncio.to_thread`)."""
    await measured("ENG-4", asyncio.to_thread(language_hooks.run, _python(_hog(150)), cwd=str(tmp_path), timeout=30))
    assert get_run_outcome("ENG-4")["peak_memory_mb"] >= 140


@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_the_graphs_gate_node_keeps_the_peak_of_the_commands_it_ran(tmp_path, monkeypatch):
    async def gate_that_builds(**kwargs):
        await asyncio.to_thread(language_hooks.run, _python(_hog(150)), cwd=str(tmp_path), timeout=30)
        return {"passed": True, "issues": [], "required_fixes": []}

    monkeypatch.setattr(graph_module, "run_deterministic_gate", gate_that_builds)

    result = await graph_module.gate_node({"run_id": "ENG-5", "worktree_path": str(tmp_path), "plan": {}, "file_changes": []})

    assert result["status"] == "verifying"
    outcome = get_run_outcome("ENG-5")
    assert outcome["peak_memory_mb"] >= 140 and outcome["gate_runs"] == 1
