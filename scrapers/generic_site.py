"""
Generic third-party manga site scraper.

Handles sites like MangaKatana where:
  - Chapters are numbered sequentially (c1, c2, chapter-3, etc.)
  - Chapter images are embedded in the page HTML / JavaScript
  - A chapter-list page lists all available chapters

Supports token-based CDN image systems (MangaKatana), direct <img> tags,
and JavaScript variable arrays — tries all extraction strategies in order.
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

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})

# ── Regex patterns for image extraction ───────────────────────────────────────

# Matches numbered page images: 0.jpg, 1.jpg, 01.jpg, 001.png etc.
_PAGE_IMG_RE = re.compile(r'(\d{1,4})\.(jpe?g|png|webp|gif)', re.IGNORECASE)

# Full CDN/token URL ending in a numbered page: .../token/ABC/0.jpg
_FULL_TOKEN_URL_RE = re.compile(
    r'https?://[^\s"\'<>]+/\d{1,4}\.(jpe?g|png|webp|gif)',
    re.IGNORECASE,
)

# MangaKatana-style token base: https://i1.mangakatana.com/token/XXXX/
_TOKEN_BASE_RE = re.compile(
    r'(https?://i\d*\.[a-z0-9_-]+\.[a-z]+/(?:token|manga)/[^"\'<>\s]+/)',
    re.IGNORECASE,
)

# JS arrays of image filenames: var pages = "0.jpg|1.jpg|2.jpg"
_JS_PAGE_ARRAY_RE = re.compile(
    r'(?:pages|imgs?|images?|chapter_images?)\s*[=:]\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# Chapter number from a URL segment like c1, c12, chapter-12, chapter_12
_CHAP_FROM_URL_RE = re.compile(
    r'(?:^|/|-)(?:c|chapter[-_]?)(\d+(?:\.\d+)?)(?:/|$|\.|#)',
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fetch(url: str, referer: str = "", retries: int = 3) -> Optional[requests.Response]:
    headers = {}
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            log.warning("Fetch attempt %s/%s failed for %s: %s", attempt, retries, url, exc)
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


# ──────────────────────────────────────────────────────────────────────────────
# Chapter discovery
# ──────────────────────────────────────────────────────────────────────────────

def _make_chapter_url(site_cfg: dict, chapter_num: float) -> str:
    """Build the chapter URL from the template and a chapter number."""
    template: str = site_cfg.get("chapter_url_template", "{base_url}/c{num}")
    base_url: str = site_cfg.get("base_url", "").rstrip("/")
    num_int = int(chapter_num) if chapter_num == int(chapter_num) else chapter_num
    return template.format(base_url=base_url, num=num_int)


def _find_latest_chapter(site_cfg: dict) -> Optional[float]:
    """
    Scrape the manga's main page (or a chapter page) to find the highest
    available chapter number.

    Strategy:
      1. Fetch base_url (the manga's chapter list page).
      2. Find all anchor/option hrefs that match the chapter URL template.
      3. Return the highest chapter number found.
    """
    base_url: str = site_cfg.get("base_url", "").rstrip("/")
    template: str = site_cfg.get("chapter_url_template", "{base_url}/c{num}")

    # Build a regex that matches chapter URLs for this specific manga
    escaped_base = re.escape(base_url)
    # Replace {num} with a capture group
    url_regex_str = template.format(base_url=escaped_base, num=r"(\d+(?:\.\d+)?)")
    url_regex = re.compile(url_regex_str, re.IGNORECASE)

    # Also try the generic chapter slug pattern as a fallback
    generic_re = re.compile(r'(?:c|chapter[-_]?)(\d+(?:\.\d+)?)', re.IGNORECASE)

    resp = _fetch(base_url)
    if not resp:
        # Try fetching a known chapter page instead (c1) to get the chapter select
        c1_url = _make_chapter_url(site_cfg, 1)
        resp = _fetch(c1_url)
        if not resp:
            log.error("Could not fetch chapter list for %s", base_url)
            return None

    page_text = resp.text
    soup = BeautifulSoup(page_text, "html.parser")

    best: Optional[float] = None

    # Search all hrefs in the page
    for tag in soup.find_all(["a", "option"], href=True):
        href = tag.get("href") or tag.get("value") or ""
        m = url_regex.search(href) or generic_re.search(href)
        if m:
            try:
                num = float(m.group(1))
                if best is None or num > best:
                    best = num
            except (ValueError, IndexError):
                pass

    # Also search <option value="..."> for chapter selects
    for opt in soup.find_all("option"):
        val = opt.get("value", "")
        m = url_regex.search(val) or generic_re.search(val)
        if m:
            try:
                num = float(m.group(1))
                if best is None or num > best:
                    best = num
            except (ValueError, IndexError):
                pass

    if best is not None:
        log.info("Latest chapter found on site: %.4g", best)
    else:
        log.warning("Could not detect latest chapter from %s", base_url)

    return best


# ──────────────────────────────────────────────────────────────────────────────
# Image extraction from a chapter page
# ──────────────────────────────────────────────────────────────────────────────

def _extract_images_from_page(html: str, page_url: str) -> list[str]:
    """
    Try multiple strategies to extract page image URLs from a chapter page.

    Returns an ordered list of image URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"

    # ── Strategy 1: <img> tags with data-src or src ───────────────────────────
    direct_imgs: list[tuple[int, str]] = []
    for img in soup.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("src")
            or ""
        )
        if not src or src.endswith(".gif") or "logo" in src.lower():
            continue
        m = _PAGE_IMG_RE.search(src)
        if m:
            page_num = int(m.group(1))
            full_url = src if src.startswith("http") else urljoin(base, src)
            direct_imgs.append((page_num, full_url))

    if direct_imgs:
        direct_imgs.sort(key=lambda x: x[0])
        log.info("Strategy 1 (img tags): found %d images", len(direct_imgs))
        return [u for _, u in direct_imgs]

    # ── Strategy 2: token CDN URLs embedded anywhere in the HTML ─────────────
    if _FULL_TOKEN_URL_RE.search(html):
        # _FULL_TOKEN_URL_RE.findall returns ext group — rebuild full URLs
        full_token_urls = _FULL_TOKEN_URL_RE.finditer(html)
        numbered: list[tuple[int, str]] = []
        seen: set[str] = set()
        for m in _FULL_TOKEN_URL_RE.finditer(html):
            url = m.group(0)
            if url in seen:
                continue
            seen.add(url)
            page_m = _PAGE_IMG_RE.search(url.split("/")[-1])
            if page_m:
                numbered.append((int(page_m.group(1)), url))
        if numbered:
            numbered.sort(key=lambda x: x[0])
            log.info("Strategy 2 (token URLs in HTML): found %d images", len(numbered))
            return [u for _, u in numbered]

    # ── Strategy 3: JS variable base_url + page array ─────────────────────────
    token_base_match = _TOKEN_BASE_RE.search(html)
    js_pages_match = _JS_PAGE_ARRAY_RE.search(html)

    if token_base_match and js_pages_match:
        token_base = token_base_match.group(1)
        pages_raw = js_pages_match.group(1)
        # Pages might be pipe-separated or comma-separated
        pages = [p.strip().strip('"\'') for p in re.split(r'[|,]', pages_raw) if p.strip()]
        pages = [p for p in pages if _PAGE_IMG_RE.search(p)]
        if pages:
            log.info("Strategy 3 (JS base+array): found %d images", len(pages))
            return [token_base + p for p in pages]

    # ── Strategy 4: Scan all script tags for numbered image references ─────────
    script_imgs: list[tuple[int, str]] = []
    for script in soup.find_all("script"):
        text = script.string or ""
        for m in _FULL_TOKEN_URL_RE.finditer(text):
            url = m.group(0)
            page_m = _PAGE_IMG_RE.search(url.split("/")[-1])
            if page_m:
                script_imgs.append((int(page_m.group(1)), url))

    if script_imgs:
        seen: set[str] = set()
        unique: list[tuple[int, str]] = []
        for num, url in script_imgs:
            if url not in seen:
                seen.add(url)
                unique.append((num, url))
        unique.sort(key=lambda x: x[0])
        log.info("Strategy 4 (script tags): found %d images", len(unique))
        return [u for _, u in unique]

    log.warning("No images found on chapter page %s", page_url)
    return []


# ──────────────────────────────────────────────────────────────────────────────
# CBZ builder
# ──────────────────────────────────────────────────────────────────────────────

def _build_cbz(pages: list[bytes], cbz_path: Path) -> None:
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cbz_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for idx, page_bytes in enumerate(pages, start=1):
            ext = _detect_ext(page_bytes)
            zf.writestr(f"{idx:03d}{ext}", page_bytes)
    tmp.rename(cbz_path)


def _cbz_filename(series_name: str, chapter_num: float) -> str:
    if chapter_num == int(chapter_num):
        num_str = f"{int(chapter_num):03d}"
    else:
        whole = int(chapter_num)
        frac = f"{round(chapter_num - whole, 3):.3f}"[1:]
        num_str = f"{whole:03d}{frac}"
    safe = re.sub(r'[\\/*?:"<>|]', "_", series_name).strip()
    return f"{safe} - Chapter {num_str}.cbz"


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def download_new_chapters(
    site_cfg: dict,
    nas_path: str,
    page_delay: float,
    chapter_delay: float,
    state: dict,
    status_callback=None,
) -> int:
    """
    Check for and download new chapters for one configured third-party site.

    Returns the number of chapters newly downloaded.
    """
    name: str = site_cfg.get("name", "Unknown")
    nas_folder: str = site_cfg.get("nas_folder") or name
    state_key: str = f"_site_{re.sub(r'[^a-z0-9]', '_', name.lower())}"

    def _status(msg: str, level: str = "info"):
        log.info(msg)
        if status_callback:
            status_callback(msg, level)

    _status(f"[{name}] Checking for new chapters...", "manga")

    series_dir = Path(nas_path) / re.sub(r'[\\/*?:"<>|]', "_", nas_folder).strip()
    series_dir.mkdir(parents=True, exist_ok=True)

    # Sync state from whatever is already on the NAS
    downloader.sync_state_from_nas(state, state_key, series_dir, name)
    state.setdefault(state_key, {})["title"] = name

    last_chapter = downloader.get_last_chapter(state, state_key)

    # Find latest chapter on the site
    latest = _find_latest_chapter(site_cfg)
    if latest is None:
        return 0

    if last_chapter is not None and latest <= last_chapter:
        _status(f"[{name}] Up to date (Ch.{int(latest)})", "uptodate")
        return 0

    # Download all chapters newer than what we have.
    # Use ceil on last_chapter so e.g. 10.5 → start at 11, not 10.
    import math
    start = math.ceil(last_chapter) + 1 if last_chapter is not None else 1
    end = int(latest)
    chapters_to_download = list(range(start, end + 1))

    _status(
        f"[{name}] {len(chapters_to_download)} new chapter(s) found (Ch.{start} - Ch.{end}), downloading...",
        "manga"
    )

    downloaded = 0
    highest = last_chapter

    for idx, chap_num in enumerate(chapters_to_download, 1):
        chap_url = _make_chapter_url(site_cfg, chap_num)
        cbz_name = _cbz_filename(name, float(chap_num))
        cbz_path = series_dir / cbz_name

        if cbz_path.exists():
            if highest is None or chap_num > highest:
                highest = float(chap_num)
            continue

        _status(
            f"[{name}] Ch.{chap_num} ({idx}/{len(chapters_to_download)}) downloading...",
            "progress"
        )

        # Fetch chapter page
        resp = _fetch(chap_url, referer=site_cfg.get("base_url", ""))
        if not resp:
            log.error("Could not fetch chapter page: %s", chap_url)
            continue

        image_urls = _extract_images_from_page(resp.text, chap_url)
        if not image_urls:
            log.error("No images found for chapter %s at %s", chap_num, chap_url)
            continue

        pages: list[bytes] = []
        failed = False

        for url in tqdm(image_urls, desc=f"Ch {chap_num}", unit="pg", leave=False):
            page_resp = _fetch(url, referer=chap_url)
            if not page_resp:
                log.error("Page download failed: %s", url)
                failed = True
                break
            pages.append(page_resp.content)
            time.sleep(page_delay)

        if failed or not pages:
            _status(f"[{name}] Ch.{chap_num} failed to download, skipping.", "error")
            continue

        try:
            _build_cbz(pages, cbz_path)
        except Exception as exc:
            log.error("Failed to write CBZ: %s", exc)
            if cbz_path.exists():
                cbz_path.unlink()
            continue

        _status(f"[{name}] Ch.{chap_num} saved ({len(pages)} pages)", "saved")
        downloaded += 1
        highest = float(chap_num)

        downloader.set_last_chapter(state, state_key, highest, name)
        downloader.record_chapter_download(state, state_key, float(chap_num))
        downloader.save_state(state)

        time.sleep(chapter_delay)

    if downloaded > 0:
        _status(f"[{name}] Done! {downloaded} chapter(s) saved.", "done")

    return downloaded
