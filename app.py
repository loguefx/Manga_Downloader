"""
Flask web application — serves the Manga Downloader dashboard and REST API.
Also runs the download scheduler in a background thread.
"""

import io
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import schedule
import yaml
from flask import Flask, jsonify, render_template, request, send_file, abort

import downloader
import scheduler as sched
import paths

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

APP_VERSION = "1.2.4"

CONFIG_PATH = paths.CONFIG_PATH
STATE_FILE  = paths.STATE_FILE

app = Flask(__name__, template_folder=paths.TEMPLATE_FOLDER, static_folder=paths.STATIC_FOLDER)
log = logging.getLogger(__name__)

_scan_lock = threading.Lock()
_scanning = False
_scan_log: list[str] = []
_MAX_LOG = 100


# ──────────────────────────────────────────────────────────────────────────────
# Config / state helpers
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = """\
# Manga Downloader — configuration file
# Edit this file or use the web UI at http://localhost:8080/config

nas_path: "C:/Manga"          # folder where CBZ chapters are saved
check_interval_hours: 6       # how often to auto-scan for new chapters
language: "en"
image_quality: "data"         # "data" = full quality, "data-saver" = compressed
max_chapters_per_run: 0       # 0 = unlimited (download until caught up)
page_delay: 0.3               # seconds between page downloads
chapter_delay: 1.0            # seconds between chapters
web_port: 8080

manga: []                     # add MangaDex manga via the Config page

third_party_sites: []         # add third-party scrapers via the Config page
"""

def _ensure_config() -> None:
    """Create a starter config.yaml next to the EXE if one does not exist."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        log.info("Created default config.yaml at %s", CONFIG_PATH)

def _load_config() -> dict:
    _ensure_config()
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save_config(data: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False)


def _load_state() -> dict:
    return downloader.load_state()


# ──────────────────────────────────────────────────────────────────────────────
# Background scheduler
# ──────────────────────────────────────────────────────────────────────────────

def _scheduler_loop() -> None:
    """Runs in a daemon thread; fires the download cycle on the configured interval."""
    cfg = _load_config()
    interval: float = float(cfg.get("check_interval_hours", 6))
    schedule.every(interval).hours.do(_trigger_scan)
    log.info("Scheduler started — will scan every %.1f hour(s).", interval)
    while True:
        schedule.run_pending()
        import time; time.sleep(60)


def _trigger_scan() -> None:
    global _scanning
    if _scanning:
        log.info("Scan already running, skipping scheduled trigger.")
        return
    thread = threading.Thread(target=_run_scan_thread, daemon=True)
    thread.start()


def _run_scan_thread() -> None:
    global _scanning, _scan_log
    with _scan_lock:
        _scanning = True
        _scan_log = []
        try:
            sched.run_download_cycle(status_callback=_make_log_cb())
        finally:
            _scanning = False


def _make_log_cb():
    """Return a status callback that appends timestamped entries to _scan_log."""
    def _log_cb(msg: str, level: str = "info"):
        entry = {
            "msg":   msg,
            "level": level,
            "time":  datetime.now().strftime("%H:%M:%S"),
        }
        _scan_log.append(entry)
        if len(_scan_log) > _MAX_LOG:
            _scan_log.pop(0)
    return _log_cb


def _run_single_scan_thread(item_id: str, source: str) -> None:
    """Download one manga / third-party site without running the full cycle."""
    global _scanning, _scan_log
    with _scan_lock:
        _scanning = True
        _scan_log = []
        try:
            cb  = _make_log_cb()
            cfg = _load_config()
            state = downloader.load_state()

            nas_path      = cfg.get("nas_path", "./manga")
            language      = cfg.get("language", "en")
            image_quality = cfg.get("image_quality", "data")
            page_delay    = float(cfg.get("page_delay_seconds", 0.5))
            chapter_delay = float(cfg.get("chapter_delay_seconds", 2))
            max_chapters  = int(cfg.get("max_chapters_per_run", 0))

            if source == "MangaDex":
                entry = next(
                    (e for e in cfg.get("manga", []) if e.get("id") == item_id), {}
                )
                downloader.download_manga(
                    manga_id=item_id,
                    config_name=entry.get("name"),
                    nas_path=nas_path,
                    language=language,
                    image_quality=image_quality,
                    page_delay=page_delay,
                    chapter_delay=chapter_delay,
                    max_chapters=max_chapters,
                    state=state,
                    status_callback=cb,
                )
            else:
                import re as _re
                site_cfg = next(
                    (s for s in cfg.get("third_party_sites", [])
                     if f"_site_{_re.sub(r'[^a-z0-9]', '_', s.get('name','').lower())}" == item_id),
                    None,
                )
                if site_cfg:
                    from scrapers import generic_site
                    generic_site.download_new_chapters(
                        site_cfg=site_cfg,
                        nas_path=nas_path,
                        page_delay=page_delay,
                        chapter_delay=chapter_delay,
                        state=state,
                        status_callback=cb,
                    )
        finally:
            _scanning = False


def start_scheduler() -> None:
    """Start the background scheduler thread. Called at app startup."""
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    t.start()


# ──────────────────────────────────────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


# ──────────────────────────────────────────────────────────────────────────────
# REST API — status
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    state = _load_state()
    meta = state.get("_meta", {})
    return jsonify({
        "last_scan": meta.get("last_scan"),
        "next_scan": meta.get("next_scan"),
        "scanning": _scanning,
        "version": APP_VERSION,
    })


# ──────────────────────────────────────────────────────────────────────────────
# REST API — manga list
# ──────────────────────────────────────────────────────────────────────────────

def _safe_folder_name(name: str) -> str:
    """Mirror the logic in downloader._safe_name / generic_site folder naming."""
    import re as _re
    return _re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def _scan_nas_series(nas_path: str, folder_name: str):
    """
    Scan a manga folder on the NAS and return:
      (cbz_count, newest_mtime_iso, highest_chapter_num)

    Falls back gracefully if the folder doesn't exist or is unreachable.
    """
    try:
        from datetime import timezone
        import re as _re
        series_dir = Path(nas_path) / folder_name
        if not series_dir.exists():
            return 0, None, None

        cbz_files = list(series_dir.glob("*.cbz"))
        if not cbz_files:
            return 0, None, None

        # Most recent file modification time
        newest_mtime = max(f.stat().st_mtime for f in cbz_files)
        newest_iso = datetime.fromtimestamp(newest_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Highest chapter number — match "Chapter 019.500" style filenames
        # by looking for the last number group preceded by "Chapter "
        _chap_re = _re.compile(r"[Cc]hapter\s+(\d+(?:\.\d+)?)", _re.IGNORECASE)
        _any_re  = _re.compile(r"(\d+(?:\.\d+)?)")
        highest = None
        for f in cbz_files:
            stem = f.stem
            m = _chap_re.search(stem) or _any_re.search(stem)
            if m:
                try:
                    n = float(m.group(1))
                    if highest is None or n > highest:
                        highest = n
                except (ValueError, OverflowError):
                    pass

        return len(cbz_files), newest_iso, highest
    except Exception:
        return 0, None, None


@app.route("/api/manga")
def api_manga():
    import re as _re
    try:
        cfg   = _load_config()
        state = _load_state()
        nas_path = cfg.get("nas_path", "")
        result = []

        def _safe_chapters(chapters_map):
            """Convert a chapters dict to a list, tolerating old string-value formats."""
            rows = []
            if not isinstance(chapters_map, dict):
                return rows
            for k, v in chapters_map.items():
                try:
                    num = float(k)
                except (ValueError, TypeError):
                    continue
                dl_at = v.get("downloaded_at", "") if isinstance(v, dict) else ""
                rows.append({"number": num, "downloaded_at": dl_at})
            return sorted(rows, key=lambda x: x["number"], reverse=True)

        # ── MangaDex entries ──────────────────────────────────────────────────
        for entry in cfg.get("manga", []):
            manga_id = entry.get("id", "").strip()
            if not manga_id:
                continue
            manga_state  = state.get(manga_id, {}) if isinstance(state.get(manga_id), dict) else {}
            chapters_map = manga_state.get("chapters", {})
            series_name  = entry.get("name") or manga_state.get("title") or manga_id
            folder_name  = _safe_folder_name(series_name)

            nas_count, nas_newest, nas_highest = _scan_nas_series(nas_path, folder_name)

            chapter_list = _safe_chapters(chapters_map)
            last_dl = chapter_list[0]["downloaded_at"] if chapter_list else (nas_newest or "")

            result.append({
                "id":              manga_id,
                "name":            series_name,
                "source":          "MangaDex",
                "total_chapters":  nas_count if nas_count > 0 else len(chapter_list),
                "latest_chapter":  manga_state.get("last_chapter") or nas_highest,
                "chapters":        chapter_list,
                "_sort_key":       last_dl,
            })

        # ── Third-party site entries ──────────────────────────────────────────
        for site in cfg.get("third_party_sites", []):
            if not site.get("enabled", False):
                continue
            site_name   = site.get("name", "Unknown")
            nas_folder  = site.get("nas_folder") or site_name
            state_key   = f"_site_{_re.sub(r'[^a-z0-9]', '_', site_name.lower())}"
            site_state  = state.get(state_key, {}) if isinstance(state.get(state_key), dict) else {}
            chapters_map = site_state.get("chapters", {})

            nas_count, nas_newest, nas_highest = _scan_nas_series(nas_path, _safe_folder_name(nas_folder))

            chapter_list = _safe_chapters(chapters_map)
            last_dl = chapter_list[0]["downloaded_at"] if chapter_list else (nas_newest or "")

            result.append({
                "id":             state_key,
                "name":           site_name,
                "source":         "3rd Party",
                "total_chapters": nas_count if nas_count > 0 else len(chapter_list),
                "latest_chapter": site_state.get("last_chapter") or nas_highest,
                "chapters":       chapter_list,
                "_sort_key":      last_dl,
            })

        # ── Sort by most recently downloaded (newest first) ───────────────────
        result.sort(key=lambda x: x.pop("_sort_key") or "", reverse=True)
        return jsonify(result)

    except Exception as exc:
        log.exception("api_manga error: %s", exc)
        return jsonify([]), 200   # always return valid JSON so the UI doesn't crash


# ──────────────────────────────────────────────────────────────────────────────
# REST API — cover images (proxied from NAS)
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/cover/<path:manga_name>")
def api_cover(manga_name):
    cfg = _load_config()
    nas_path = cfg.get("nas_path", "")
    cover_path = Path(nas_path) / manga_name / "folder.jpg"
    if cover_path.exists():
        return send_file(cover_path, mimetype="image/jpeg")
    abort(404)


# ──────────────────────────────────────────────────────────────────────────────
# REST API — manual scan trigger
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/scan-log")
def api_scan_log():
    return jsonify({"log": _scan_log, "scanning": _scanning})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if _scanning:
        return jsonify({"success": False, "message": "A scan is already in progress."})
    thread = threading.Thread(target=_run_scan_thread, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Scan started."})


@app.route("/api/scan/single", methods=["POST"])
def api_scan_single():
    """Trigger a download for one specific manga (by id + source)."""
    if _scanning:
        return jsonify({"success": False, "message": "A scan is already in progress."})
    data    = request.get_json(force=True) or {}
    item_id = data.get("id", "").strip()
    source  = data.get("source", "")
    if not item_id:
        return jsonify({"success": False, "message": "No id provided."}), 400
    thread = threading.Thread(
        target=_run_single_scan_thread, args=(item_id, source), daemon=True
    )
    thread.start()
    return jsonify({"success": True})


@app.route("/api/search/mangadex")
def api_search_mangadex():
    """Proxy a MangaDex title search — used by the Config page add-manga UI."""
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])
    try:
        import mangadex_api as api
        results = api.search_manga(query, limit=12)
        return jsonify(results)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# REST API — config CRUD
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(_load_config())


@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"success": False, "message": "No data received."}), 400
    _save_config(data)
    # Reschedule with new interval
    schedule.clear()
    interval = float(data.get("check_interval_hours", 6))
    schedule.every(interval).hours.do(_trigger_scan)
    return jsonify({"success": True})


# ──────────────────────────────────────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────────────────────────────────────

def run_flask(host: str = "0.0.0.0", port: int = None) -> None:
    cfg = _load_config()
    port = port or int(cfg.get("web_port", 8080))
    app.run(host=host, port=port, use_reloader=False, threaded=True)


def _setup_logging(console: bool = True) -> None:
    handlers = [logging.FileHandler(paths.LOG_FILE, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def _run_normal() -> None:
    """Start Flask + scheduler in normal console mode (fallback / dev mode)."""
    _setup_logging(console=True)
    cfg = _load_config()
    port = int(cfg.get("web_port", 8080))
    log.info("Starting Manga Downloader web UI on http://localhost:%d", port)
    log.info("Press Ctrl+C to stop.")
    start_scheduler()
    run_flask(port=port)


def _is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _auto_service_start() -> None:
    """
    Default behaviour when the EXE is launched with no arguments:
      1. Already running  -> print status and exit cleanly.
      2. Installed, stopped -> start it and exit.
      3. Not installed     -> install (auto-start on boot) then start.

    If not running as Administrator, show a clear message and exit — no loop,
    no fallback to console mode.
    """
    import time
    _setup_logging(console=True)

    # ── Require admin before doing anything ──────────────────────────────────
    if not _is_admin():
        print("")
        print("  Manga Downloader needs Administrator privileges to install")
        print("  the Windows Service.")
        print("")
        print("  Right-click MangaDownloader.exe and choose")
        print("  'Run as administrator', then try again.")
        print("")
        time.sleep(4)
        sys.exit(1)

    try:
        import win32service
        import win32serviceutil
        import pywintypes
        from service import MangaDownloaderService
    except ImportError:
        log.error("pywin32 is missing — cannot manage Windows Service.")
        sys.exit(1)

    svc_name = MangaDownloaderService._svc_name_
    exe      = sys.executable

    # ── Check current service state ───────────────────────────────────────────
    is_installed = False
    is_running   = False
    try:
        status       = win32serviceutil.QueryServiceStatus(svc_name)
        is_installed = True
        is_running   = status[1] == win32service.SERVICE_RUNNING
    except pywintypes.error:
        is_installed = False

    if is_installed and is_running:
        log.info("Manga Downloader service is already running.")
        log.info("Dashboard: http://localhost:8080")
        sys.exit(0)

    # ── Install if not present ────────────────────────────────────────────────
    if not is_installed:
        log.info("Installing Manga Downloader Windows Service...")
        # Use a subprocess so HandleCommandLine's internal sys.exit() doesn't
        # terminate this process. Pass ONLY "install" so the child's sys.argv[1]
        # is "install" — this routes it straight to HandleCommandLine and avoids
        # the infinite loop that "--startup auto install" caused.
        try:
            result = subprocess.run([exe, "install"], timeout=30)
            if result.returncode != 0:
                log.error("Service install failed (exit code %d).", result.returncode)
                sys.exit(1)
            log.info("Service installed successfully (auto-start on reboot).")
        except Exception as exc:
            log.error("Service install failed: %s", exc)
            sys.exit(1)

        # ── Open firewall port so the dashboard is reachable on the LAN ──────
        cfg = _load_config()
        port = int(cfg.get("web_port", 8080))
        try:
            subprocess.run(
                [
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    f"name=MangaDownloader-{port}",
                    "dir=in",
                    "action=allow",
                    "protocol=TCP",
                    f"localport={port}",
                    "profile=private,domain",
                ],
                timeout=15,
                check=True,
            )
            log.info("Firewall rule added — port %d open on private network.", port)
        except Exception as exc:
            log.warning("Could not add firewall rule automatically: %s", exc)
            log.warning("If the dashboard is unreachable from other devices, run this manually as Admin:")
            log.warning("  netsh advfirewall firewall add rule name=MangaDownloader dir=in action=allow protocol=TCP localport=%d", port)

    # ── Start the service ─────────────────────────────────────────────────────
    try:
        log.info("Starting Manga Downloader service...")
        win32serviceutil.StartService(svc_name)
        log.info("Service started!  Dashboard -> http://localhost:8080")
        log.info("Starts automatically on every reboot.")
        log.info("")
        log.info("Other commands (run as Administrator):")
        log.info("  MangaDownloader.exe stop    - stop the service")
        log.info("  MangaDownloader.exe restart - restart the service")
        log.info("  MangaDownloader.exe remove  - uninstall the service")
        time.sleep(2)
        sys.exit(0)
    except Exception as exc:
        log.error("Could not start service: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    _SVC_COMMANDS = {"install", "remove", "start", "stop", "restart", "debug", "update"}

    if len(sys.argv) > 1 and sys.argv[1].lower() == "--dev":
        # Direct / development run — skip service machinery entirely
        _setup_logging(console=True)
        cfg  = _load_config()
        port = cfg.get("web_port", 8080)
        log.info("Dev mode: starting scheduler + Flask on port %s", port)
        start_scheduler()
        run_flask(host="0.0.0.0", port=port)
    elif len(sys.argv) > 1 and sys.argv[1].lower() in _SVC_COMMANDS:
        # Explicit service management command (install / remove / stop / etc.)
        try:
            import win32serviceutil
            from service import MangaDownloaderService
            win32serviceutil.HandleCommandLine(MangaDownloaderService)
        except ImportError:
            print("ERROR: pywin32 is not available. Cannot manage Windows Service.")
            sys.exit(1)
    else:
        # No arguments: could be Windows SCM starting the service, OR user
        # double-clicking the EXE.
        #
        # Try to register with SCM first. If SCM started us, this succeeds
        # and the service runs properly in the background.
        #
        # If the user ran it manually, servicemanager.Initialize() raises
        # error 1063 (ERROR_FAILED_SERVICE_CONTROLLER_CONNECT) — we catch
        # that and fall through to _auto_service_start() instead.
        try:
            import servicemanager
            from service import MangaDownloaderService
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(MangaDownloaderService)
            servicemanager.StartServiceCtrlDispatcher()
        except ImportError:
            # pywin32 not available at all
            _auto_service_start()
        except Exception:
            # Error 1063 = not started by SCM = user ran the EXE directly
            _auto_service_start()
