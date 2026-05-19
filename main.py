"""
CLI entry point.

Usage:
  python main.py               # run once and exit (good for Task Scheduler)
  python main.py --daemon      # stay running, poll on schedule (no web UI)
  python main.py --add <url>   # add a manga by URL/UUID to config.yaml

For the full web dashboard + background service, use:
  python app.py                # web UI on http://localhost:8080
"""

import argparse
import logging
import re
import sys
import time

import schedule
import yaml

import scheduler as sched
from paths import CONFIG_PATH, LOG_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def add_manga_to_config(url_or_id: str) -> None:
    match = _UUID_RE.search(url_or_id)
    if not match:
        log.error("Could not find a MangaDex UUID in: %s", url_or_id)
        sys.exit(1)

    manga_id = match.group(0)

    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    existing_ids = {e.get("id", "") for e in cfg.get("manga", [])}
    if manga_id in existing_ids:
        log.info("Manga %s is already in config.yaml - nothing to add.", manga_id)
        return

    import mangadex_api as api
    log.info("Fetching manga info for %s...", manga_id)
    try:
        info = api.get_manga_info(manga_id)
        title = api.get_manga_title(info)
    except Exception as exc:
        log.error("Could not fetch manga info: %s", exc)
        title = ""

    new_entry = {"id": manga_id}
    if title:
        new_entry["name"] = title
        log.info("Title: %s", title)

    cfg.setdefault("manga", []).append(new_entry)

    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, allow_unicode=True, sort_keys=False)

    log.info("Added %s (%s) to config.yaml", title or manga_id, manga_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manga Downloader CLI — use 'python app.py' for the web dashboard."
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Stay running and poll on schedule (no web UI)",
    )
    parser.add_argument(
        "--add",
        metavar="URL_OR_ID",
        help="Add a manga by MangaDex URL or UUID to config.yaml, then exit",
    )
    args = parser.parse_args()

    if args.add:
        add_manga_to_config(args.add)
        return

    if args.daemon:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        interval_hours: float = float(cfg.get("check_interval_hours", 6))
        log.info("Daemon mode - checking every %.1f hour(s). Press Ctrl+C to stop.", interval_hours)
        sched.run_download_cycle()
        schedule.every(interval_hours).hours.do(sched.run_download_cycle)
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Stopped.")
        return

    # Default: run once
    sched.run_download_cycle()


if __name__ == "__main__":
    main()
