"""
Core download cycle.
Imported by both app.py (daemon/web mode) and main.py (CLI mode).
"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

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
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


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
    return total_downloaded
