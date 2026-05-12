"""
Core download cycle.
Imported by both app.py (daemon/web mode) and main.py (CLI mode).
"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

import downloader
from scrapers import generic_site

from paths import CONFIG_PATH
log = logging.getLogger(__name__)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


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
    language: str = cfg.get("language", "en")
    image_quality: str = cfg.get("image_quality", "data")
    page_delay: float = float(cfg.get("page_delay_seconds", 0.5))
    chapter_delay: float = float(cfg.get("chapter_delay_seconds", 2))
    max_chapters: int = int(cfg.get("max_chapters_per_run", 0))
    manga_list: list = cfg.get("manga", [])

    state = downloader.load_state()

    # Record scan start time
    now_str = datetime.now().isoformat(timespec="seconds")
    state.setdefault("_meta", {})["last_scan"] = now_str
    interval_hours = float(cfg.get("check_interval_hours", 6))
    next_scan = datetime.now() + timedelta(hours=interval_hours)
    state["_meta"]["next_scan"] = next_scan.isoformat(timespec="seconds")
    downloader.save_state(state)

    total_downloaded = 0

    # ── MangaDex manga ────────────────────────────────────────────────────────
    for entry in manga_list:
        manga_id: str = entry.get("id", "").strip()
        config_name: str = entry.get("name", "").strip() or None

        if not manga_id:
            log.warning("Skipping entry with missing 'id': %s", entry)
            continue

        try:
            count = downloader.download_manga(
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
            )
            total_downloaded += count
        except Exception as exc:
            log.exception("Unexpected error processing manga %s: %s", manga_id, exc)

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
            )
            total_downloaded += count
        except Exception as exc:
            log.exception("Third-party scraper error for %s: %s", site_name, exc)

    _status(f"Scan complete — {total_downloaded} new chapter(s) downloaded total.", "done")

    # ── Post-scan integrations ────────────────────────────────────────────────
    if total_downloaded > 0:
        _send_discord_notification(cfg, state)
        _trigger_komga_scan(cfg, _status)

    return total_downloaded


# ── Integration helpers ───────────────────────────────────────────────────────

def _send_discord_notification(cfg: dict, state: dict) -> None:
    """Post a Discord embed listing every newly downloaded chapter."""
    webhook_url = cfg.get("discord_webhook_url", "").strip()
    if not webhook_url:
        return

    # Collect chapter download records from state, grouped by manga name
    lines = []
    for key, val in state.items():
        if key.startswith("_"):
            continue
        if not isinstance(val, dict):
            continue
        title = val.get("title") or key
        chapters_map = val.get("chapters", {})
        if not chapters_map:
            continue
        # Only include chapters downloaded in the last hour
        recent = []
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        for chap_num, chap_data in chapters_map.items():
            if isinstance(chap_data, dict) and chap_data.get("downloaded_at", "") >= cutoff:
                recent.append(float(chap_num))
        if recent:
            recent.sort()
            chap_str = ", ".join(
                str(int(c) if c == int(c) else c) for c in recent
            )
            lines.append({"name": title, "value": f"Ch. {chap_str}", "inline": True})

    if not lines:
        return

    total = sum(1 for f in lines for _ in [f])
    payload = {
        "embeds": [{
            "title": "Manga Downloader — New Chapters Ready",
            "color": 0xe53935,
            "fields": lines[:25],
            "footer": {
                "text": f"{len(lines)} series updated • {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            },
        }]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info("Discord notification sent.")
        else:
            log.warning("Discord webhook returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Could not send Discord notification: %s", exc)


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
