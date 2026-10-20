"""elstin/selftest.py — the check a packaged build runs on itself."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from elstin import selftest

ROOT = Path(__file__).resolve().parent.parent


def test_a_whole_build_passes_every_check_and_says_so():
    done = subprocess.run([sys.executable, "-m", "elstin.launcher", "--self-test"], cwd=ROOT, capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, done.stdout + done.stderr
    verdict = json.loads(done.stdout.strip().splitlines()[-1].removeprefix("ELSTIN_SELFTEST "))
    assert verdict == {"ok": True, "checks": len(selftest._CHECKS), "failed": 0, "frozen": False}
    assert done.stdout.count("\nok ") + done.stdout.startswith("ok ") == len(selftest._CHECKS)


def test_a_missing_library_is_named(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(selftest, "_LIBRARIES", (*selftest._LIBRARIES, "a_library_that_was_left_out"))

    with pytest.raises(AssertionError, match="a_library_that_was_left_out"):
        selftest.check_the_libraries_it_reaches_for_are_there()


def test_a_module_that_cannot_import_is_named(monkeypatch: pytest.MonkeyPatch):
    import importlib

    real = importlib.import_module

    def broken(name, *args, **kwargs):
        if name == "elstin.services.redaction":
            raise ImportError("No module named 'a_native_extension'")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", broken)

    with pytest.raises(AssertionError, match=r"elstin\.services\.redaction.*a_native_extension"):
        selftest.check_every_elstin_module_imports()


def test_a_build_that_lost_its_ui_or_migrations_says_which_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import elstin.resources

    monkeypatch.setattr(elstin.resources, "resource_root", lambda: tmp_path)

    with pytest.raises(AssertionError, match=r"settings-ui/app\.html is missing"):
        selftest.check_the_ui_and_the_migrations_are_where_they_should_be()


def test_it_leaves_the_environment_as_it_found_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/mine.db")

    assert selftest.run() == 0

    import os

    assert os.environ["DATABASE_URL"] == f"sqlite:///{tmp_path}/mine.db"
    assert not (tmp_path / "mine.db").exists()  # it used a scratch database, not the caller's
