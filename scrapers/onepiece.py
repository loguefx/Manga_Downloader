"""
One Piece web scraper.

Scrapes a configurable chapter-list page to find the latest chapter number,
then downloads all page images and packages them into a CBZ file.

The scraper is site-agnostic as long as:
  - Chapter links on the list page contain the chapter number (e.g. "chapter-1181")
  - The chapter detail page contains <img> tags pointing to the actual page images
"""

import logging
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

import downloader

log = logging.getLogger(__name__)

STATE_KEY = "_onepiece_scraper"

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

# Patterns for extracting chapter numbers from URL slugs or link text
_CHAP_SLUG_RE = re.compile(r"chapter[-_](\d+(?:\.\d+)?)", re.IGNORECASE)
_CHAP_TEXT_RE = re.compile(r"chapter\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fetch(url: str, retries: int = 3) -> Optional[requests.Response]:
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            log.warning("Fetch error attempt %s/%s for %s: %s", attempt, retries, url, exc)
            time.sleep(5 * attempt)
    return None


def _detect_ext(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _build_cbz(pages: list[bytes], cbz_path: Path) -> None:
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cbz_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for idx, page_bytes in enumerate(pages, start=1):
            ext = _detect_ext(page_bytes)
            zf.writestr(f"{idx:03d}{ext}", page_bytes)
    tmp.rename(cbz_path)


def _cbz_filename(chapter_num: float) -> str:
    if chapter_num == int(chapter_num):
        num_str = f"{int(chapter_num):04d}"
    else:
        whole = int(chapter_num)
        frac = f"{round(chapter_num - whole, 3):.3f}"[1:]
        num_str = f"{whole:04d}{frac}"
    return f"One Piece - Chapter {num_str}.cbz"


# ──────────────────────────────────────────────────────────────────────────────
# Chapter list scraping
# ──────────────────────────────────────────────────────────────────────────────

def _find_latest_chapter(chapter_list_url: str) -> Optional[tuple[float, str]]:
    """
    Fetch the chapter list page and find the highest chapter number.

    Returns (chapter_num, chapter_url) or None on failure.
    """
    resp = _fetch(chapter_list_url)
    if not resp:
        log.error("Could not fetch chapter list from %s", chapter_list_url)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    base = f"{urlparse(chapter_list_url).scheme}://{urlparse(chapter_list_url).netloc}"

    best_num: Optional[float] = None
    best_url: Optional[str] = None

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        # Try matching chapter number from URL slug first
        m = _CHAP_SLUG_RE.search(href)
        if not m:
            # Try matching from link text
            m = _CHAP_TEXT_RE.search(a.get_text())
        if not m:
            continue
        try:
            num = float(m.group(1))
        except ValueError:
            continue

        if best_num is None or num > best_num:
            best_num = num
            best_url = urljoin(base, href)

    if best_num is None:
        log.error("No chapter links found on %s", chapter_list_url)
        return None

    log.info("Latest One Piece chapter found on site: %.4g", best_num)
    return best_num, best_url


# ──────────────────────────────────────────────────────────────────────────────
# Chapter page image scraping
# ──────────────────────────────────────────────────────────────────────────────

# Image CDN domains commonly used by One Piece sites
_CDN_DOMAINS = {
    "cdn.onepiecechapters.com",
    "cdn.readonepiece.com",
    "cdn.mangapill.com",
    "s1.mbcdn.net",
    "s2.mbcdn.net",
}


def _find_chapter_images(chapter_url: str) -> list[str]:
    """
    Fetch a chapter detail page and return the list of page image URLs in order.
    """
    resp = _fetch(chapter_url)
    if not resp:
        log.error("Could not fetch chapter page: %s", chapter_url)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    images: list[str] = []

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src:
            continue

        parsed = urlparse(src)

        # Accept images from known CDN domains or any domain if URL contains image ext
        is_cdn = any(domain in parsed.netloc for domain in _CDN_DOMAINS)
        has_img_ext = re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", src, re.IGNORECASE)

        if is_cdn or has_img_ext:
            # Skip tiny thumbnails / icons (usually < 50px referenced in style)
            width = img.get("width", "")
            if width and str(width).isdigit() and int(width) < 100:
                continue
            if src not in images:
                images.append(src)

    if not images:
        log.warning("No images found on chapter page %s", chapter_url)

    log.info("Found %d page image(s) for this chapter", len(images))
    return images


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def download_latest(
    scraper_cfg: dict,
    nas_path: str,
    image_quality: str,
    page_delay: float,
    chapter_delay: float,
    state: dict,
) -> int:
    """
    Check for and download new One Piece chapters from the configured site.

    Returns the number of chapters newly downloaded.
    """
    chapter_list_url: str = scraper_cfg.get("chapter_list_url", "")
    nas_folder: str = scraper_cfg.get("nas_folder", "One Piece")

    if not chapter_list_url:
        log.error("One Piece scraper: chapter_list_url is not configured")
        return 0

    log.info("-" * 60)
    log.info("One Piece scraper: checking %s", chapter_list_url)

    series_dir = Path(nas_path) / nas_folder
    series_dir.mkdir(parents=True, exist_ok=True)

    # Sync state from NAS (handles pre-existing c1147.cbz style files too)
    downloader.sync_state_from_nas(state, STATE_KEY, series_dir, "One Piece")
    state.setdefault(STATE_KEY, {})["title"] = "One Piece"

    last_chapter = downloader.get_last_chapter(state, STATE_KEY)
    if last_chapter is not None:
        log.info("Highest chapter on NAS: %.4g", last_chapter)
    else:
        log.info("No existing chapters detected on NAS")

    result = _find_latest_chapter(chapter_list_url)
    if not result:
        return 0
    latest_num, chapter_url = result

    if last_chapter is not None and latest_num <= last_chapter:
        log.info("One Piece is up to date (latest=%.4g, have=%.4g)", latest_num, last_chapter)
        return 0

    log.info("New chapter available: %.4g -> downloading from %s", latest_num, chapter_url)

    cbz_name = _cbz_filename(latest_num)
    cbz_path = series_dir / cbz_name

    if cbz_path.exists():
        log.info("[SKIP] %s already exists on disk", cbz_name)
        downloader.set_last_chapter(state, STATE_KEY, latest_num, "One Piece")
        downloader.save_state(state)
        return 0

    # Get page images
    image_urls = _find_chapter_images(chapter_url)
    if not image_urls:
        log.error("No images found for chapter %.4g - cannot download", latest_num)
        return 0

    # Download pages
    pages: list[bytes] = []
    failed = False
    for url in tqdm(image_urls, desc=f"  Ch {latest_num:.4g}", unit="pg", leave=False):
        resp = _fetch(url)
        if not resp:
            log.error("Failed to download page: %s", url)
            failed = True
            break
        pages.append(resp.content)
        time.sleep(page_delay)

    if failed or not pages:
        log.error("Chapter %.4g download aborted", latest_num)
        return 0

    # Build CBZ
    try:
        _build_cbz(pages, cbz_path)
    except Exception as exc:
        log.error("Failed to write CBZ for chapter %.4g: %s", latest_num, exc)
        if cbz_path.exists():
            cbz_path.unlink()
        return 0

    log.info("Saved: %s (%d pages)", cbz_path, len(pages))

    downloader.set_last_chapter(state, STATE_KEY, latest_num, "One Piece")
    downloader.record_chapter_download(state, STATE_KEY, latest_num)
    downloader.save_state(state)

    return 1
