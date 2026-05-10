"""
Centralised path resolution.

Works correctly both when running as a plain Python script and when bundled
by PyInstaller into a Windows EXE.

  - User data  (config.yaml, state.json, log) lives next to the EXE so the
    user can edit it without unpacking the bundle.
  - Bundled assets (templates/, static/, scrapers/) are extracted by
    PyInstaller into sys._MEIPASS at runtime.
"""

import sys
from pathlib import Path


def _exe_dir() -> Path:
    """Return the directory that contains the running EXE (or script)."""
    if hasattr(sys, "_MEIPASS"):          # PyInstaller one-dir bundle
        return Path(sys.executable).parent
    return Path(__file__).parent


def _bundle_dir() -> Path:
    """Return the directory that contains bundled read-only assets."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)         # type: ignore[attr-defined]
    return Path(__file__).parent


# Directories
EXE_DIR    = _exe_dir()
BUNDLE_DIR = _bundle_dir()

# User-editable / runtime-generated files (sit next to the EXE)
CONFIG_PATH = EXE_DIR / "config.yaml"
STATE_FILE  = EXE_DIR / "state.json"
LOG_FILE    = EXE_DIR / "manga_downloader.log"

# Flask asset directories (bundled read-only)
TEMPLATE_FOLDER = str(BUNDLE_DIR / "templates")
STATIC_FOLDER   = str(BUNDLE_DIR / "static")
