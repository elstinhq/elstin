"""elstin/resources.py — where the UI and the migrations are, from a checkout and from the Mac app's bundle."""
from __future__ import annotations

import sys
from pathlib import Path

from elstin.resources import resource_root


def test_from_a_checkout_the_files_are_beside_the_package():
    root = resource_root()

    assert (root / "alembic.ini").is_file() and (root / "alembic" / "env.py").is_file()
    assert (root / "settings-ui" / "app.html").is_file()
    assert (root / "elstin").is_dir()


def test_from_the_frozen_bundle_they_are_where_pyinstaller_unpacks_them(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)

    assert resource_root() == tmp_path / "_internal"


def test_the_app_and_the_migrations_both_find_them_through_it():
    import elstin.app
    import elstin.db.migrate

    assert elstin.app._UI_DIR == resource_root() / "settings-ui"
    assert elstin.db.migrate._ROOT == resource_root()
