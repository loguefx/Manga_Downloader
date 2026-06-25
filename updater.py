"""
In-app updater for Manga Downloader.

Checks GitHub Releases for a newer MangaDownloader.exe, downloads it with live
progress, then swaps the running EXE and restarts the Windows Service.

Only functional when running as the frozen PyInstaller EXE (ideally installed as
the "MangaDownloader" Windows Service). In plain `python app.py` dev mode the
install step is a no-op that reports back an explanatory message.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

import paths

log = logging.getLogger(__name__)

SERVICE_NAME = "MangaDownloader"
EXE_NAME     = "MangaDownloader.exe"
ASSET_NAME   = "MangaDownloader.exe"   # release asset to download

# Shared progress state, read by the /api/update/progress endpoint.
_lock = threading.RLock()
_progress = {
    "state": "idle",      # idle | downloading | installing | restarting | done | error
    "percent": 0,
    "downloaded": 0,
    "total": 0,
    "message": "",
    "version": "",
}


def _set(**kw) -> None:
    with _lock:
        _progress.update(kw)


def get_progress() -> dict:
    with _lock:
        return dict(_progress)


# Cached result of the most recent update check (shared by startup check + UI).
_check_cache = {"result": None, "ts": 0.0}
_check_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Version helpers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple:
    """Turn 'v1.2.22' or '1.2.22' into a comparable tuple (1, 2, 22)."""
    v = (v or "").strip().lstrip("vV")
    parts = []
    for chunk in v.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def is_frozen() -> bool:
    """True when running as the bundled PyInstaller EXE."""
    return getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")


# ──────────────────────────────────────────────────────────────────────────────
# GitHub release lookup
# ──────────────────────────────────────────────────────────────────────────────

def check_for_update(current_version: str, repo: str) -> dict:
    """
    Query the GitHub 'latest release' and compare it to current_version.

    Returns a dict:
      {update_available, current, latest, notes, download_url, size, error?}
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        r = requests.get(url, timeout=15, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Update check failed: %s", exc)
        return {"update_available": False, "current": current_version,
                "latest": current_version, "error": str(exc)}

    latest_tag = data.get("tag_name", "") or data.get("name", "")
    notes      = data.get("body", "") or ""

    download_url = None
    size = 0
    for asset in data.get("assets", []):
        if asset.get("name", "").lower() == ASSET_NAME.lower():
            download_url = asset.get("browser_download_url")
            size = asset.get("size", 0)
            break

    available = bool(latest_tag) and is_newer(latest_tag, current_version) and bool(download_url)

    return {
        "update_available": available,
        "current": current_version,
        "latest": latest_tag.lstrip("vV"),
        "notes": notes[:1500],
        "download_url": download_url,
        "size": size,
        "has_asset": bool(download_url),
    }


def get_cached_check(current_version: str, repo: str,
                     max_age: float = 600.0, force: bool = False) -> dict:
    """
    Return a cached update-check result if it's fresh, otherwise hit GitHub and
    cache the new result. Used by the dashboard banner so opening the UI doesn't
    make a GitHub round-trip every time.
    """
    with _check_lock:
        cached = _check_cache["result"]
        age = time.time() - _check_cache["ts"]
        if cached and not force and age < max_age and cached.get("current") == current_version:
            return cached

    result = check_for_update(current_version, repo)
    with _check_lock:
        _check_cache["result"] = result
        _check_cache["ts"] = time.time()
    return result


def startup_check(current_version: str, repo: str) -> None:
    """Run one update check shortly after boot and cache it. Logs the outcome."""
    try:
        info = get_cached_check(current_version, repo, force=True)
        if info.get("error"):
            log.info("Startup update check failed: %s", info["error"])
        elif info.get("update_available"):
            log.info("Update available: v%s (current v%s)",
                     info.get("latest"), current_version)
        else:
            log.info("Update check: already on the latest version (v%s).",
                     current_version)
    except Exception as exc:
        log.warning("Startup update check error: %s", exc)


def _backup_user_data(exe_dir: Path) -> None:
    """
    Copy config.yaml / state.json / secrets.yaml into a timestamped backup folder
    before swapping the EXE. The updater never overwrites these files, but this is
    a safety net so manga lists / settings are always recoverable.
    """
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = exe_dir / "backups" / f"pre_update_{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for name in ("config.yaml", "state.json", "secrets.yaml"):
            src = exe_dir / name
            if src.exists():
                shutil.copy2(src, backup_dir / name)
        log.info("Backed up user data to %s", backup_dir)
    except Exception as exc:
        log.warning("Could not back up user data before update: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Download + install
# ──────────────────────────────────────────────────────────────────────────────

def start_update(current_version: str, repo: str) -> dict:
    """
    Kick off a background download+install. Returns immediately.
    Use get_progress() to track it.
    """
    with _lock:
        if _progress["state"] in ("downloading", "installing", "restarting"):
            return {"started": False, "message": "An update is already in progress."}
        _progress.update(state="downloading", percent=0, downloaded=0, total=0,
                         message="Starting update…", version="")

    if not is_frozen():
        _set(state="error",
             message="Updates can only be installed when running the packaged EXE / "
                     "Windows Service, not in dev mode.")
        return {"started": False, "message": _progress["message"]}

    t = threading.Thread(target=_run_update, args=(current_version, repo),
                         daemon=True, name="updater")
    t.start()
    return {"started": True, "message": "Update started."}


def _run_update(current_version: str, repo: str) -> None:
    try:
        info = check_for_update(current_version, repo)
        if info.get("error"):
            _set(state="error", message=f"Could not reach GitHub: {info['error']}")
            return
        if not info.get("update_available"):
            _set(state="done", percent=100,
                 message="Already on the latest version.")
            return

        download_url = info["download_url"]
        latest       = info["latest"]
        _set(version=latest, message=f"Downloading v{latest}…")

        exe_path = Path(sys.executable)            # the running EXE
        exe_dir  = exe_path.parent
        new_path = exe_dir / "MangaDownloader.new.exe"

        # ── Download with progress ───────────────────────────────────────────
        with requests.get(download_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            _set(total=total)
            downloaded = 0
            with new_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    pct = int(downloaded * 100 / total) if total else 0
                    _set(downloaded=downloaded, percent=pct)

        # Basic sanity check: a valid Windows EXE starts with 'MZ'.
        with new_path.open("rb") as fh:
            if fh.read(2) != b"MZ":
                new_path.unlink(missing_ok=True)
                _set(state="error", message="Downloaded file is not a valid EXE.")
                return

        _set(state="installing", percent=100,
             message="Download complete. Backing up settings…")

        _backup_user_data(exe_dir)

        _set(message="Preparing to install…")
        _spawn_swap_script(exe_path, new_path)

        _set(state="restarting",
             message=f"Installing v{latest} and restarting the service…")

    except Exception as exc:
        log.exception("Update failed: %s", exc)
        _set(state="error", message=f"Update failed: {exc}")


def _spawn_swap_script(exe_path: Path, new_path: Path) -> None:
    """
    Write and launch a detached batch script that stops the service, swaps the
    EXE, and restarts the service. The script outlives this process.
    """
    exe_dir   = exe_path.parent
    old_path  = exe_dir / "MangaDownloader.old.exe"
    bat_path  = exe_dir / "_update.bat"
    log_path  = exe_dir / "_update.log"

    bat = f"""@echo off
echo [%date% %time%] Update starting > "{log_path}"
echo Stopping service... >> "{log_path}"
net stop {SERVICE_NAME} >> "{log_path}" 2>&1
rem Fallback for non-service installs
taskkill /f /im {EXE_NAME} >> "{log_path}" 2>&1

echo Waiting for EXE to unlock... >> "{log_path}"
:waitunlock
del "{old_path}" >nul 2>&1
ren "{exe_path}" "MangaDownloader.old.exe" >nul 2>&1
if exist "{exe_path}" (
    ping 127.0.0.1 -n 3 >nul
    goto waitunlock
)

echo Installing new version... >> "{log_path}"
move /Y "{new_path}" "{exe_path}" >> "{log_path}" 2>&1
del "{old_path}" >nul 2>&1

echo Restarting service... >> "{log_path}"
net start {SERVICE_NAME} >> "{log_path}" 2>&1

echo [%date% %time%] Update complete >> "{log_path}"
del "%~f0"
"""
    bat_path.write_text(bat, encoding="utf-8")

    DETACHED_PROCESS         = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW         = 0x08000000

    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
        cwd=str(exe_dir),
    )
    log.info("Update swap script launched: %s", bat_path)
