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
import updater

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

APP_VERSION = "1.2.22"

# GitHub repo used for the in-app updater (public releases).
GITHUB_REPO = "loguefx/Manga_Downloader"

CONFIG_PATH  = paths.CONFIG_PATH
SECRETS_PATH = paths.SECRETS_PATH
STATE_FILE   = paths.STATE_FILE

app = Flask(__name__, template_folder=paths.TEMPLATE_FOLDER, static_folder=paths.STATIC_FOLDER)
log = logging.getLogger(__name__)


@app.context_processor
def _inject_version():
    """Make the app version available to every template as {{ app_version }}."""
    return {"app_version": APP_VERSION}

_scan_lock = threading.Lock()
_scanning = False
_scan_log: list[dict] = []
_MAX_LOG = 100


# ──────────────────────────────────────────────────────────────────────────────
# Config / state helpers
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = """\
# Manga Downloader — configuration file
# Edit this file or use the web UI at http://localhost:8080/config

nas_path: "C:/Manga"          # primary folder where CBZ chapters are saved
additional_nas_paths: []      # extra NAS paths to search for existing series
new_manga_nas_path: ""        # where NEW manga go (empty = use nas_path)
check_interval_hours: 6       # how often to auto-scan for new chapters
language: "en"
image_quality: "data"         # "data" = full quality, "data-saver" = compressed
max_chapters_per_run: 0       # 0 = unlimited (download until caught up)
page_delay_seconds: 0.3       # seconds between page downloads
chapter_delay_seconds: 1.0    # seconds between chapters
web_port: 8080

discord_webhook_url: ""    # paste your Discord webhook URL here to enable notifications

manga: []                     # add MangaDex manga via the Config page

third_party_sites: []         # add third-party scrapers via the Config page

webtoon_series: []            # add Webtoon series via the Config page
"""

def _ensure_config() -> None:
    """Write a starter config.yaml next to the EXE if one does not exist.

    When running from a PyInstaller bundle the seed config.yaml (with the
    full manga list baked in at build time) is extracted to sys._MEIPASS.
    We copy that file so the server gets every series on first launch.
    Falls back to the hardcoded _DEFAULT_CONFIG only if no bundle seed exists.
    """
    if CONFIG_PATH.exists():
        return

    # Prefer the seed config bundled into the EXE by PyInstaller
    seed: Path | None = None
    if hasattr(sys, "_MEIPASS"):
        candidate = Path(sys._MEIPASS) / "config.yaml"
        if candidate.exists():
            seed = candidate

    if seed:
        import shutil
        shutil.copy2(seed, CONFIG_PATH)
        log.info("Deployed bundled config.yaml to %s", CONFIG_PATH)
    else:
        CONFIG_PATH.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        log.info("Created default config.yaml at %s", CONFIG_PATH)

# Keys that must NEVER be written to the committed config.yaml. They are stored
# in a local-only secrets.yaml (git-ignored, not bundled) so they can't leak via
# the public repo or the seeded EXE.
_SECRET_KEYS = ("discord_webhook_url", "komga_password", "komga_username")


def _load_config() -> dict:
    _ensure_config()
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    # Overlay any locally-stored secrets so the running app has full settings.
    if SECRETS_PATH.exists():
        try:
            with SECRETS_PATH.open("r", encoding="utf-8") as fh:
                secrets = yaml.safe_load(fh) or {}
            for k, v in secrets.items():
                if v:
                    cfg[k] = v
        except Exception as exc:
            log.warning("Could not read secrets.yaml: %s", exc)

    return cfg


def _save_config(data: dict) -> None:
    data = dict(data)  # shallow copy so we don't mutate the caller's dict

    # Split secret fields into secrets.yaml; strip them from config.yaml.
    secrets = {}
    for k in _SECRET_KEYS:
        if k in data:
            val = data.pop(k)
            if val:
                secrets[k] = val

    # Merge with any secrets already on disk so we never lose existing values.
    existing_secrets = {}
    if SECRETS_PATH.exists():
        try:
            with SECRETS_PATH.open("r", encoding="utf-8") as fh:
                existing_secrets = yaml.safe_load(fh) or {}
        except Exception:
            existing_secrets = {}
    existing_secrets.update(secrets)

    if existing_secrets:
        with SECRETS_PATH.open("w", encoding="utf-8") as fh:
            yaml.dump(existing_secrets, fh, allow_unicode=True, sort_keys=False)

    # config.yaml: blank out secret keys so the committed/bundled file is clean.
    for k in _SECRET_KEYS:
        data[k] = ""

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
            # Persist the log so other clients (and future restarts) can see it
            try:
                state = downloader.load_state()
                state.setdefault("_meta", {})["last_scan_log"] = list(_scan_log)
                downloader.save_state(state)
            except Exception as exc:
                log.warning("Could not persist scan log to state: %s", exc)


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
            search_paths  = downloader.get_nas_search_paths(cfg) or [nas_path]
            new_manga_path = downloader.get_new_manga_nas_path(cfg) or nas_path
            language      = cfg.get("language", "en")
            image_quality = cfg.get("image_quality", "data")
            page_delay    = float(cfg.get("page_delay_seconds", 0.5))
            chapter_delay = float(cfg.get("chapter_delay_seconds", 2))
            max_chapters  = int(cfg.get("max_chapters_per_run", 0))

            chapters_downloaded = 0
            if source == "MangaDex":
                entry = next(
                    (e for e in cfg.get("manga", []) if e.get("id") == item_id), {}
                )
                count, _title, _chaps = downloader.download_manga(
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
                    search_paths=search_paths,
                    new_manga_path=new_manga_path,
                )
                chapters_downloaded = count
            else:
                import re as _re
                site_cfg = next(
                    (s for s in cfg.get("third_party_sites", [])
                     if f"_site_{_re.sub(r'[^a-z0-9]', '_', s.get('name','').lower())}" == item_id),
                    None,
                )
                if site_cfg:
                    from scrapers import generic_site
                    chapters_downloaded = generic_site.download_new_chapters(
                        site_cfg=site_cfg,
                        nas_path=nas_path,
                        page_delay=page_delay,
                        chapter_delay=chapter_delay,
                        state=state,
                        status_callback=cb,
                        search_paths=search_paths,
                        new_manga_path=new_manga_path,
                    )

            # ── Post-download: trigger Komga rescan ───────────────────────────
            if chapters_downloaded and chapters_downloaded > 0:
                cb(f"Triggering Komga rescan after {chapters_downloaded} new chapter(s)...", "info")
                from scheduler import _trigger_komga_scan
                _trigger_komga_scan(cfg, cb)
            else:
                cb("No new chapters found — Komga scan skipped.", "info")

        finally:
            _scanning = False


def start_scheduler() -> None:
    """Start the background scheduler thread. Called at app startup."""
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    t.start()
    # Pre-warm cover URLs for all manga that don't have one cached yet
    pw = threading.Thread(target=_prefetch_cover_urls, daemon=True, name="cover-prefetch")
    pw.start()


def _prefetch_cover_urls() -> None:
    """Run once on startup: fetch and cache CDN cover URLs for any manga
    that doesn't already have one in state.json.  Runs in a background thread
    so it never blocks the server from starting."""
    import time
    time.sleep(5)  # let the server finish starting before hitting MangaDex
    try:
        import mangadex_api as api
        cfg   = _load_config()
        state = downloader.load_state()
        missing = [
            e.get("id", "").strip()
            for e in cfg.get("manga", [])
            if e.get("id", "").strip()
            and not state.get(e.get("id", "").strip(), {}).get("cover_url")
        ]
        if not missing:
            log.info("Cover prefetch: all covers already cached.")
            return
        log.info("Cover prefetch: fetching URLs for %d manga...", len(missing))
        updated = 0
        for manga_id in missing:
            try:
                url = api.get_cover_url(manga_id, quality="256")
                if url:
                    state.setdefault(manga_id, {})["cover_url"] = url
                    updated += 1
                time.sleep(0.3)   # be polite to MangaDex
            except Exception as exc:
                log.debug("Cover prefetch failed for %s: %s", manga_id, exc)
        if updated:
            downloader.save_state(state)
            log.info("Cover prefetch: cached %d new cover URLs.", updated)
    except Exception as exc:
        log.warning("Cover prefetch error: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


@app.route("/browse")
def browse_page():
    return render_template("browse.html")


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
# REST API — self-update
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/update/check")
def api_update_check():
    info = updater.check_for_update(APP_VERSION, GITHUB_REPO)
    info["frozen"] = updater.is_frozen()
    return jsonify(info)


@app.route("/api/update/install", methods=["POST"])
def api_update_install():
    result = updater.start_update(APP_VERSION, GITHUB_REPO)
    code = 200 if result.get("started") else 409
    return jsonify(result), code


@app.route("/api/update/progress")
def api_update_progress():
    return jsonify(updater.get_progress())


# ──────────────────────────────────────────────────────────────────────────────
# REST API — manga list
# ──────────────────────────────────────────────────────────────────────────────

def _safe_folder_name(name: str) -> str:
    """Mirror the logic in downloader._safe_name / generic_site folder naming."""
    import re as _re
    return _re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def _scan_nas_series(nas_paths, folder_name: str):
    """
    Scan a manga folder across one or more NAS paths and return:
      (cbz_count, newest_mtime_iso, highest_chapter_num)

    nas_paths may be a single path string or a list of paths. The first path
    that contains the series folder is used.

    Falls back gracefully if the folder doesn't exist or is unreachable.
    """
    try:
        from datetime import timezone
        import re as _re

        if isinstance(nas_paths, str):
            nas_paths = [nas_paths]

        series_dir = None
        for base in nas_paths:
            if not base:
                continue
            candidate = Path(base) / folder_name
            try:
                if candidate.exists():
                    series_dir = candidate
                    break
            except OSError:
                continue
        if series_dir is None:
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
        search_paths = downloader.get_nas_search_paths(cfg) or [nas_path]
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

            nas_count, nas_newest, nas_highest = _scan_nas_series(search_paths, folder_name)

            chapter_list = _safe_chapters(chapters_map)
            last_dl = chapter_list[0]["downloaded_at"] if chapter_list else (nas_newest or "")

            result.append({
                "id":              manga_id,
                "name":            series_name,
                "source":          "MangaDex",
                "total_chapters":  nas_count if nas_count > 0 else len(chapter_list),
                "latest_chapter":  manga_state.get("last_chapter") or nas_highest,
                "chapters":        chapter_list,
                "cover_url":       manga_state.get("cover_url"),
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

            nas_count, nas_newest, nas_highest = _scan_nas_series(search_paths, _safe_folder_name(nas_folder))

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
    from flask import redirect
    cfg  = _load_config()
    nas_path = cfg.get("nas_path", "")
    search_paths = downloader.get_nas_search_paths(cfg) or [nas_path]

    # Try every NAS path (safe name then raw name)
    for base in search_paths:
        for folder in [downloader._safe_name(manga_name), manga_name]:
            p = Path(base) / folder / "folder.jpg"
            try:
                if p.exists():
                    return send_file(p, mimetype="image/jpeg")
            except OSError:
                continue

    # NAS not reachable — look up the cached CDN URL from state
    state = downloader.load_state()
    for manga_id, entry in state.items():
        if manga_id.startswith("_"):
            continue
        if isinstance(entry, dict) and entry.get("title") == manga_name:
            cdn = entry.get("cover_url")
            if cdn:
                return redirect(cdn)

    # Last resort: match by config name → manga_id → fetch CDN URL live
    try:
        import mangadex_api as api
        for entry in cfg.get("manga", []):
            if entry.get("name", "") == manga_name:
                cdn = api.get_cover_url(entry["id"], quality="512")
                if cdn:
                    # Cache it for next time
                    state.setdefault(entry["id"], {})["cover_url"] = cdn
                    downloader.save_state(state)
                    return redirect(cdn)
    except Exception:
        pass

    abort(404)


# ──────────────────────────────────────────────────────────────────────────────
# REST API — manual scan trigger
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/scan-log")
def api_scan_log():
    # If no in-memory log yet (e.g. fresh server restart), return the last
    # persisted scan log so the second computer still sees meaningful data.
    if _scan_log:
        return jsonify({"log": _scan_log, "scanning": _scanning})
    state = _load_state()
    persisted = state.get("_meta", {}).get("last_scan_log", [])
    return jsonify({"log": persisted, "scanning": _scanning})


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


@app.route("/api/komga/scan", methods=["POST"])
def api_komga_scan():
    """Manually trigger a Komga library rescan."""
    try:
        from scheduler import _trigger_komga_scan
        cfg = _load_config()
        _trigger_komga_scan(cfg)
        return jsonify({"success": True, "message": "Komga scan triggered."})
    except Exception as exc:
        log.exception("api_komga_scan error: %s", exc)
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/test/discord", methods=["POST"])
def api_test_discord():
    """Send a test Discord notification using the currently-configured webhook URL."""
    try:
        import requests as _requests
        data = request.get_json(force=True) or {}
        webhook_url = (
            data.get("discord_webhook_url")
            or _load_config().get("discord_webhook_url", "")
        ).strip()

        if not webhook_url:
            return jsonify({"success": False, "message": "No webhook URL configured."}), 400

        payload = {
            "embeds": [{
                "title": "✅ Manga Downloader — Test Notification",
                "description": (
                    "Your Discord webhook is connected and working!\n"
                    "You'll receive a message like this after each scan that finds new chapters."
                ),
                "color": 0x43a047,
                "footer": {
                    "text": f"Manga Downloader v{APP_VERSION} • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                },
            }]
        }
        resp = _requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            return jsonify({"success": True, "message": "Test message sent — check your Discord channel!"})
        else:
            return jsonify({
                "success": False,
                "message": f"Discord returned HTTP {resp.status_code}: {resp.text[:200]}",
            }), 400
    except Exception as exc:
        log.exception("api_test_discord error: %s", exc)
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/browse/mangadex")
def api_browse_mangadex():
    """Browse / search MangaDex with sorting and pagination for the Browse page."""
    query  = request.args.get("q", "").strip()
    sort   = request.args.get("sort", "popular")
    try:
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0
    try:
        import mangadex_api as api
        # Also check which manga are already in config so we can mark them
        cfg         = _load_config()
        existing_ids = {m.get("id", "") for m in cfg.get("manga", [])}
        data        = api.browse_manga(query=query, sort=sort, offset=offset, limit=24)
        for r in data["results"]:
            r["in_library"] = r["id"] in existing_ids
        return jsonify(data)
    except Exception as exc:
        log.exception("api_browse_mangadex error: %s", exc)
        return jsonify({"results": [], "total": 0, "offset": offset})


@app.route("/api/check/mangadex/<manga_id>")
def api_check_mangadex(manga_id: str):
    """Return whether a manga has chapters actually downloadable via MangaDex."""
    try:
        import mangadex_api as api
        cfg      = _load_config()
        language = cfg.get("language", "en")
        ok, reason = api.has_downloadable_chapters(manga_id, language=language)
        return jsonify({"downloadable": ok, "reason": reason})
    except Exception as exc:
        log.exception("api_check_mangadex error: %s", exc)
        return jsonify({"downloadable": False, "reason": str(exc)})


@app.route("/api/refresh-metadata", methods=["POST"])
def api_refresh_metadata():
    """
    Walk every series folder on the NAS and write ComicInfo.xml if missing.
    Runs in a background thread; returns immediately with a job-started message.
    """
    import comicinfo as ci_mod
    from comicinfo import ComicInfoData
    import mangadex_api as mapi

    cfg      = _load_config()
    nas_path = cfg.get("nas_path", "")
    search_paths = downloader.get_nas_search_paths(cfg) or [nas_path]
    if not nas_path:
        return jsonify({"success": False, "message": "NAS path not configured."}), 400

    def _run():
        manga_list     = cfg.get("manga", [])
        webtoon_list   = cfg.get("webtoon_series", [])
        written = 0
        skipped = 0

        # ── MangaDex series ──────────────────────────────────────────────────
        for entry in manga_list:
            manga_id    = entry.get("id", "").strip()
            config_name = entry.get("name", "").strip() or None
            if not manga_id:
                continue
            try:
                manga_info  = mapi.get_manga_info(manga_id)
                series_name = config_name or mapi.get_manga_title(manga_info)
                series_dir  = downloader.find_existing_series_dir(series_name, search_paths)
                if series_dir is None:
                    continue
                ci_path = series_dir / "ComicInfo.xml"
                if ci_path.exists():
                    skipped += 1
                    continue
                meta    = mapi.get_manga_metadata(manga_info)
                ci_data = ComicInfoData(
                    series     = series_name,
                    summary    = meta.get("summary", ""),
                    genres     = meta.get("genres", ""),
                    tags       = meta.get("tags", ""),
                    writer     = meta.get("writer", ""),
                    penciller  = meta.get("penciller", ""),
                    age_rating = meta.get("age_rating", ""),
                    language   = cfg.get("language", "en"),
                )
                ci_mod.write_sidecar(series_dir, ci_data)
                log.info("[meta-refresh] Wrote ComicInfo.xml for %s", series_name)
                written += 1
                import time as _t; _t.sleep(0.4)   # polite MangaDex rate limit
            except Exception as exc:
                log.warning("[meta-refresh] Failed for %s: %s", manga_id, exc)

        # ── Webtoon series ───────────────────────────────────────────────────
        from scrapers import webtoon as wt
        for wt_cfg in webtoon_list:
            if not wt_cfg.get("enabled", True):
                continue
            wt_name  = wt_cfg.get("name", "")
            wt_url   = wt_cfg.get("url", "")
            wt_folder= wt_cfg.get("nas_folder") or wt_name
            series_dir = downloader.find_existing_series_dir(wt_folder, search_paths)
            if series_dir is None:
                continue
            ci_path = series_dir / "ComicInfo.xml"
            if ci_path.exists():
                skipped += 1
                continue
            try:
                meta    = wt.get_series_metadata(wt_url)
                ci_data = ComicInfoData(
                    series    = wt_name,
                    summary   = meta.get("summary", ""),
                    genres    = meta.get("genres", ""),
                    tags      = meta.get("tags", ""),
                    writer    = meta.get("writer", ""),
                    publisher = "Webtoon",
                    language  = "en",
                )
                ci_mod.write_sidecar(series_dir, ci_data)
                log.info("[meta-refresh] Wrote ComicInfo.xml for %s", wt_name)
                written += 1
            except Exception as exc:
                log.warning("[meta-refresh] Failed for webtoon %s: %s", wt_name, exc)

        log.info("[meta-refresh] Done. Written: %d, Already existed: %d", written, skipped)

    threading.Thread(target=_run, daemon=True, name="meta-refresh").start()
    return jsonify({"success": True, "message": "Metadata refresh started in background."})


@app.route("/api/cleanup/blocked", methods=["POST"])
def api_cleanup_blocked():
    """
    Scan every MangaDex manga in the library.  Any whose chapters are all
    external / blocked gets removed from config, deleted from the NAS, and
    purged from state.json.

    Returns a summary of what was removed and what was kept.
    """
    import shutil
    import mangadex_api as api

    cfg      = _load_config()
    state    = downloader.load_state()
    language = cfg.get("language", "en")
    nas_path = cfg.get("nas_path", "./manga")
    search_paths = downloader.get_nas_search_paths(cfg) or [nas_path]

    manga_list = cfg.get("manga", [])
    removed: list[dict] = []
    kept:    list[dict] = []
    errors:  list[str]  = []

    for entry in manga_list:
        manga_id = entry.get("id", "").strip()
        name     = entry.get("name", "") or manga_id
        if not manga_id:
            kept.append(entry)
            continue

        try:
            ok, reason = api.has_downloadable_chapters(manga_id, language=language)
        except Exception as exc:
            log.warning("cleanup: check failed for %s (%s): %s", name, manga_id, exc)
            errors.append(f"Check failed for {name}: {exc}")
            kept.append(entry)
            continue

        if ok:
            kept.append(entry)
            continue

        # ── Blocked: purge NAS folder (wherever it lives) ──────────────────
        series_dir = downloader.find_existing_series_dir(name, search_paths)
        nas_deleted = False
        if series_dir is not None:
            try:
                shutil.rmtree(series_dir)
                nas_deleted = True
                log.info("cleanup: deleted NAS folder %s", series_dir)
            except Exception as exc:
                errors.append(f"Could not delete folder for {name}: {exc}")
                log.warning("cleanup: could not rmtree %s: %s", series_dir, exc)

        # ── Purge from state.json ──────────────────────────────────────────
        state.pop(manga_id, None)

        removed.append({
            "id":          manga_id,
            "name":        name,
            "reason":      reason,
            "nas_deleted": nas_deleted,
        })
        log.info("cleanup: removed blocked manga %s (%s)", name, manga_id)

    if removed:
        cfg["manga"] = kept
        _save_config(cfg)
        downloader.save_state(state)
        log.info("cleanup: removed %d blocked manga, kept %d", len(removed), len(kept))

    return jsonify({
        "success": True,
        "removed": removed,
        "kept_count": len(kept),
        "errors":  errors,
    })


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


def _purge_stale_service(svc_name: str) -> None:
    """
    If a previous service registration is stuck in 'marked for deletion' state,
    force-remove it via `sc delete` and wait for Windows to clean up the handle
    before we attempt a fresh install.  Silently does nothing if the service is
    healthy or not present at all.
    """
    import time
    import pywintypes
    import win32service
    import win32serviceutil

    try:
        status = win32serviceutil.QueryServiceStatus(svc_name)
        # SERVICE_RUNNING (4) – don't touch it
        if status[1] == win32service.SERVICE_RUNNING:
            return
    except pywintypes.error:
        # Service not installed – nothing to clean up
        return

    # Stop the service if it is still in a transient state
    try:
        win32serviceutil.StopService(svc_name)
        time.sleep(2)
    except Exception:
        pass

    # Force-delete via sc.exe so the registry key is released immediately
    result = subprocess.run(
        ["sc", "delete", svc_name],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        print(f"Cleaned up stale '{svc_name}' service registration.")
        # Give SCM a moment to release its internal handles
        time.sleep(3)
    else:
        # Not fatal – HandleCommandLine will surface a clear error if it fails
        print(f"Warning: sc delete returned {result.returncode}: {result.stdout.strip()}")


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
        # Clean up any stale "marked for deletion" registration before installing
        _purge_stale_service(svc_name)
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
        app.config["TEMPLATES_AUTO_RELOAD"] = True   # only in dev
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

            # When installing, pre-clean any service that is "marked for deletion"
            # (Windows holds the registry entry until all SCM handles are closed).
            if sys.argv[1].lower() == "install":
                _purge_stale_service(MangaDownloaderService._svc_name_)

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
