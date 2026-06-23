"""
Core download cycle.
Imported by both app.py (daemon/web mode) and main.py (CLI mode).
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

import downloader
from scrapers import generic_site, webtoon as webtoon_scraper

from paths import CONFIG_PATH, SECRETS_PATH
log = logging.getLogger(__name__)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    # Overlay local-only secrets (Discord webhook, Komga creds) that are kept
    # out of the committed config.yaml.
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


def run_download_cycle(status_callback=None) -> int:
    """
    Run one full download cycle across all configured manga sources.

    Parameters
    ----------
    status_callback : callable(str) | None
        If provided, called with a status string as each stage completes.
        Used by the web UI to stream progress.

    Returns total chapters downloaded.
    """
    def _status(msg: str, level: str = "info"):
        log.info(msg)
        if status_callback:
            status_callback(msg, level)

    _status("Scan started — checking all sources...", "header")

    cfg = load_config()
    nas_path: str = cfg.get("nas_path", "./manga")
    search_paths: list = downloader.get_nas_search_paths(cfg) or [nas_path]
    new_manga_path: str = downloader.get_new_manga_nas_path(cfg) or nas_path
    language: str = cfg.get("language", "en")
    image_quality: str = cfg.get("image_quality", "data")
    page_delay: float = float(cfg.get("page_delay_seconds", 0.5))
    chapter_delay: float = float(cfg.get("chapter_delay_seconds", 2))
    max_chapters: int = int(cfg.get("max_chapters_per_run", 0))
    manga_list: list = cfg.get("manga", [])

    if len(search_paths) > 1:
        _status(
            f"NAS paths: searching {len(search_paths)} location(s); "
            f"new manga → {new_manga_path}",
            "info"
        )

    state = downloader.load_state()

    # Record scan start time
    now_str = datetime.now().isoformat(timespec="seconds")
    state.setdefault("_meta", {})["last_scan"] = now_str
    interval_hours = float(cfg.get("check_interval_hours", 6))
    next_scan = datetime.now() + timedelta(hours=interval_hours)
    state["_meta"]["next_scan"] = next_scan.isoformat(timespec="seconds")
    downloader.save_state(state)

    total_downloaded = 0

    # ── MangaDex manga (parallel workers) ────────────────────────────────────
    # Up to 5 manga are checked concurrently.  Each worker calls download_manga
    # which uses _state_lock internally for all state mutations, so concurrent
    # writes are safe.  Page/chapter downloads are I/O-bound (network + NAS)
    # so threading provides real speed-up without hitting GIL limits.
    _WORKERS = 5

    valid_entries = [
        (e.get("id", "").strip(), e.get("name", "").strip() or None)
        for e in manga_list
        if e.get("id", "").strip()
    ]
    skipped = len(manga_list) - len(valid_entries)
    if skipped:
        log.warning("Skipping %d manga entries with missing 'id'.", skipped)

    # Keyed by series title → sorted list of chapter numbers actually saved this run
    scan_downloads: dict[str, list[float]] = {}

    def _check_one(manga_id: str, config_name):
        try:
            return downloader.download_manga(
                manga_id=manga_id,
                config_name=config_name,
                nas_path=nas_path,
                language=language,
                image_quality=image_quality,
                page_delay=page_delay,
                chapter_delay=chapter_delay,
                max_chapters=max_chapters,
                state=state,
                status_callback=status_callback,
                search_paths=search_paths,
                new_manga_path=new_manga_path,
            )
        except Exception as exc:
            log.exception("Unexpected error processing manga %s: %s", manga_id, exc)
            return 0, "", []

    with ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="scan") as pool:
        futures = {
            pool.submit(_check_one, mid, name): mid
            for mid, name in valid_entries
        }
        for fut in as_completed(futures):
            count, title, chapters = fut.result()
            total_downloaded += count
            if chapters and title:
                scan_downloads[title] = sorted(chapters)

    # ── Third-party website scrapers ──────────────────────────────────────────
    for site_cfg in cfg.get("third_party_sites", []):
        if not site_cfg.get("enabled", False):
            continue
        site_name = site_cfg.get("name", "Unknown")
        log.info("Running third-party scraper: %s...", site_name)
        try:
            count = generic_site.download_new_chapters(
                site_cfg=site_cfg,
                nas_path=nas_path,
                page_delay=page_delay,
                chapter_delay=chapter_delay,
                state=state,
                status_callback=status_callback,
                search_paths=search_paths,
                new_manga_path=new_manga_path,
            )
            total_downloaded += count
        except Exception as exc:
            log.exception("Third-party scraper error for %s: %s", site_name, exc)

    # ── Webtoon series ─────────────────────────────────────────────────────────
    for wt_cfg in cfg.get("webtoon_series", []):
        if not wt_cfg.get("enabled", True):
            continue
        wt_name = wt_cfg.get("name", "Unknown Webtoon")
        try:
            count, title, chapters = webtoon_scraper.download_new_episodes(
                site_cfg=wt_cfg,
                nas_path=nas_path,
                page_delay=page_delay,
                chapter_delay=chapter_delay,
                state=state,
                status_callback=status_callback,
                search_paths=search_paths,
                new_manga_path=new_manga_path,
            )
            total_downloaded += count
            if chapters and title:
                scan_downloads[title] = sorted(set(scan_downloads.get(title, []) + chapters))
        except Exception as exc:
            log.exception("Webtoon scraper error for %s: %s", wt_name, exc)

    _status(f"Scan complete — {total_downloaded} new chapter(s) downloaded total.", "done")

    # ── Post-scan integrations ────────────────────────────────────────────────
    if total_downloaded > 0:
        _send_discord_notification(cfg, scan_downloads, status_callback=_status)
        _trigger_komga_scan(cfg, _status)
    else:
        if cfg.get("discord_webhook_url", "").strip():
            _status("Discord: no new chapters — notification skipped.", "info")

    return total_downloaded


# ── Integration helpers ───────────────────────────────────────────────────────

def _send_discord_notification(cfg: dict, scan_downloads: dict, status_callback=None) -> None:
    """Post a Discord embed listing every chapter actually downloaded in this scan.

    scan_downloads: {series_title: [sorted chapter nums]} — built directly by
    run_download_cycle so it only contains chapters saved in this run.
    """
    def _status(msg, level="info"):
        if status_callback:
            status_callback(msg, level)
        log.info(msg)

    webhook_url = cfg.get("discord_webhook_url", "").strip()
    if not webhook_url:
        return

    lines = []
    for title, chapters in scan_downloads.items():
        if not chapters:
            continue
        chap_str = ", ".join(
            str(int(c)) if c == int(c) else str(c) for c in chapters
        )
        lines.append({"name": title, "value": f"Ch. {chap_str}", "inline": True})

    if not lines:
        _status("Discord: no new chapters — notification skipped.", "info")
        return

    total_series  = len(lines)
    total_chapters = sum(
        len(f["value"].replace("Ch. ", "").split(", ")) for f in lines
    )

    payload = {
        "embeds": [{
            "title": "📖 Manga Downloader — New Chapters Ready!",
            "color": 0xe53935,
            "fields": lines[:25],   # Discord hard limit: 25 fields per embed
            "footer": {
                "text": (
                    f"{total_series} series • {total_chapters} chapter(s) downloaded"
                    f" • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                )
            },
        }]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            _status(f"Discord: notification sent — {total_series} series, {total_chapters} chapter(s).", "done")
        else:
            _status(f"Discord: webhook error {resp.status_code} — {resp.text[:120]}", "error")
    except Exception as exc:
        _status(f"Discord: failed to send notification — {exc}", "error")


def _trigger_komga_scan(cfg: dict, _status=None) -> None:
    """Trigger a Komga library rescan via the Komga REST API.

    Komga requires a library ID in the URL.  We first fetch all libraries the
    configured user can see, then POST /scan on each one.  If the user has
    configured a specific komga_library_id we only scan that one.
    """
    komga_url  = cfg.get("komga_url", "").strip().rstrip("/")
    komga_user = cfg.get("komga_username", "").strip()
    komga_pass = cfg.get("komga_password", "").strip()
    specific_id = cfg.get("komga_library_id", "").strip()

    if not komga_url:
        return

    auth = (komga_user, komga_pass) if komga_user else None
    headers = {"Accept": "application/json"}

    try:
        if specific_id:
            library_ids = [specific_id]
        else:
            # Fetch all library IDs
            r = requests.get(
                f"{komga_url}/api/v1/libraries",
                auth=auth, headers=headers, timeout=15,
            )
            r.raise_for_status()
            library_ids = [lib["id"] for lib in r.json().get("content", r.json() if isinstance(r.json(), list) else [])]

        if not library_ids:
            log.warning("Komga: no libraries found to scan.")
            return

        for lib_id in library_ids:
            resp = requests.post(
                f"{komga_url}/api/v1/libraries/{lib_id}/scan",
                auth=auth, headers=headers, timeout=15,
            )
            if resp.status_code in (200, 202, 204):
                log.info("Komga library %s scan triggered.", lib_id)
            else:
                log.warning("Komga scan library %s returned %d: %s", lib_id, resp.status_code, resp.text[:200])

        if _status:
            _status(f"Komga library scan triggered ({len(library_ids)} lib(s)).", "info")

    except Exception as exc:
        log.warning("Could not trigger Komga scan: %s", exc)
