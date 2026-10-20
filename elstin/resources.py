"""Where the files that ship beside the code live: `settings-ui/` (the web UI) and
`alembic/` with `alembic.ini` (the migrations).

From a checkout (or the Docker image, which copies them next to `elstin/`) that is
the repository root. In the Mac app the code is frozen into a bundle by PyInstaller
and the same directories sit inside it, at `sys._MEIPASS`.
"""
from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent
