"""PyInstaller's entry script for the backend (packaging/elstin-backend.spec).

A script of its own rather than `elstin/launcher.py`, so the frozen program starts
as `__main__` the way any entry script does, and the package stays an ordinary
import."""
import sys

from elstin.launcher import main

sys.exit(main())
