# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Mac app's backend: `elstin-backend/elstin-backend`, the
# launcher (elstin/launcher.py) frozen with its own Python and libraries, plus the UI
# and the migrations it serves and runs. Built by packaging/build-backend.sh, from a
# clean copy of the source (ROOT below) with the build venv's packages installed.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # SPECPATH: the directory of this file, set by PyInstaller

# What the app reads at runtime, beside the code (elstin/resources.py finds it again at
# sys._MEIPASS): the web UI, and Alembic's scripts and config.
datas = [
    (str(ROOT / "settings-ui"), "settings-ui"),
    (str(ROOT / "alembic"), "alembic"),
    (str(ROOT / "alembic.ini"), "."),
]

# Imports PyInstaller's analysis cannot see: uvicorn picks its loop, protocol and
# lifespan classes by name; SQLAlchemy picks its dialect by name from the URL.
# Every elstin module goes in whether or not something imports it by statement, so
# "a module is missing" cannot happen in the one place nobody tests (the self-test
# imports them all). elstin.mcp is the standalone MCP servers, which need a package
# the app does not use.
hiddenimports = (
    collect_submodules("elstin", filter=lambda name: not name.startswith("elstin.mcp"))
    + collect_submodules("uvicorn")
    + ["sqlalchemy.dialects.sqlite.pysqlite", "aiosqlite", "langgraph.checkpoint.sqlite.aio"]
)

analysis = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "elstin.mcp", "mcp"],
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="elstin-backend",
    console=True,         # a child of the window, whose stdout is the protocol (elstin/launcher.py)
    strip=False,          # stripping would invalidate the signatures Phase 4 puts on
    upx=False,
    target_arch="arm64",
    codesign_identity=None,  # signed afterwards, inside-out, with the hardened runtime
)
COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="elstin-backend")
