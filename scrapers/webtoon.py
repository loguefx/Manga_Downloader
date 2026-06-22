"""
Webtoon scraper for webtoons.com

Series URL format (paste this into the Config page):
  https://www.webtoons.com/en/{genre}/{slug}/list?title_no=XXXXX

How it works:
  1. Parse title_no from the series URL.
  2. Fetch episode list pages (paginated, 10 episodes per page) to discover all
     episode numbers newer than the last downloaded episode.
  3. For each new episode, fetch the viewer page and extract image URLs from
     <img class="_images" data-url="..."> tags.
  4. Download images with the required Referer header and package as CBZ.

Notes:
  - Webtoon blocks image downloads without Referer: https://www.webtoons.com
  - Episode numbers (episode_no) are used as chapter numbers in our state.
  - Episodes are listed newest-first on each list page; we reverse to get oldest-first.
"""

import logging
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

import downloader
import comicinfo as ci_mod
from comicinfo import ComicInfoData

log = logging.getLogger(__name__)

_WEBTOON_BASE  = "https://www.webtoons.com"
_WEBTOON_CDN   = "https://webtoon-phinf.pstatic.net"

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": _WEBTOON_BASE,
})


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def _cbz_filename(series_name: str, episode_no: int) -> str:
    return f"{_safe_name(series_name)} - Chapter {episode_no:03d}.cbz"


def _detect_ext(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"GIF":
        return ".gif"
    return ".jpg"


def _build_cbz(pages: list[bytes], cbz_path: Path) -> None:
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cbz_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for idx, page_bytes in enumerate(pages, start=1):
            ext = _detect_ext(page_bytes)
            zf.writestr(f"{idx:03d}{ext}", page_bytes)
    tmp.rename(cbz_path)


def _fetch(url: str, referer: str = _WEBTOON_BASE, retries: int = 4) -> Optional[requests.Response]:
    headers = {"Referer": referer}
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, headers=headers, timeout=30)
            # Webtoon returns 429 Too Many Requests or 403 when rate-limited.
            # Back off generously before retrying.
            if resp.status_code == 429:
                wait = 30 * attempt
                log.warning("Webtoon rate-limited (429) — backing off %ds (attempt %d/%d)", wait, attempt, retries)
                time.sleep(wait)
                continue
            if resp.status_code == 403:
                wait = 20 * attempt
                log.warning("Webtoon 403 Forbidden — backing off %ds (attempt %d/%d)", wait, attempt, retries)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            log.warning("Webtoon fetch attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            time.sleep(10 * attempt)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Title number extraction
# ──────────────────────────────────────────────────────────────────────────────

def parse_title_no(series_url: str) -> Optional[str]:
    """Extract title_no from a Webtoon series URL."""
    qs = parse_qs(urlparse(series_url).query)
    if "title_no" in qs:
        return qs["title_no"][0]
    # Try matching from path as fallback
    m = re.search(r'title_no[=_](\d+)', series_url)
    return m.group(1) if m else None


def parse_base_list_url(series_url: str) -> str:
    """Return the canonical /list URL for this series (strip episode path)."""
    parsed = urlparse(series_url)
    # Remove episode viewer segment — keep only up to the slug's list page
    parts = parsed.path.rstrip("/").split("/")
    # Expected: ['', 'en', genre, slug, 'list'] or similar
    # Strip any 'viewer' segment and everything after the slug
    if "list" in parts:
        list_idx = parts.index("list")
        clean_path = "/".join(parts[:list_idx + 1])
    else:
        # Use the path as-is, append /list
        clean_path = parsed.path.rstrip("/") + "/list" if "viewer" not in parsed.path else "/".join(parts[:-1]) + "/list"
    title_no = parse_title_no(series_url)
    base = f"{parsed.scheme}://{parsed.netloc}{clean_path}"
    if title_no:
        base += f"?title_no={title_no}"
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Episode discovery
# ──────────────────────────────────────────────────────────────────────────────

def get_episode_list(series_url: str) -> list[dict]:
    """
    Return all episodes for a series, sorted by episode_no ascending.

    Each entry: {"episode_no": int, "url": str, "title": str}

    Webtoon paginates episodes (10 per page, newest first).
    We keep fetching pages until we get an empty page.
    """
    title_no = parse_title_no(series_url)
    list_url  = parse_base_list_url(series_url)
    episodes: dict[int, dict] = {}  # episode_no → entry (dedup)

    page = 1
    while True:
        url = f"{list_url}&page={page}" if "?" in list_url else f"{list_url}?page={page}"
        resp = _fetch(url)
        if not resp:
            log.error("Could not fetch episode list page %d for %s", page, list_url)
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # Primary: <li> items inside the episode list
        found_on_page = 0
        for a in soup.select("ul#_listUl li a, ul.lst_episode li a, .detail_lst li a"):
            href = a.get("href", "")
            if "episode_no=" not in href:
                continue
            ep_qs = parse_qs(urlparse(href).query)
            ep_no_str = ep_qs.get("episode_no", [None])[0]
            if ep_no_str is None:
                continue
            try:
                ep_no = int(ep_no_str)
            except ValueError:
                continue

            # Try to get a display title
            subj = a.select_one(".subj span, .subj, .tx_subject")
            title = subj.get_text(strip=True) if subj else f"Episode {ep_no}"

            full_url = href if href.startswith("http") else urljoin(_WEBTOON_BASE, href)
            if ep_no not in episodes:
                episodes[ep_no] = {"episode_no": ep_no, "url": full_url, "title": title}
                found_on_page += 1

        if found_on_page == 0:
            break   # no more pages
        page += 1
        time.sleep(0.5)  # be polite

    result = sorted(episodes.values(), key=lambda e: e["episode_no"])
    log.info("Webtoon: found %d episode(s) total for title_no=%s", len(result), title_no)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Image extraction from viewer page
# ──────────────────────────────────────────────────────────────────────────────

def get_episode_images(episode_url: str) -> list[str]:
    """
    Fetch the viewer page and extract all image URLs for the episode.

    Webtoon uses <img class="_images" data-url="..."> tags.
    Images must be downloaded with Referer: https://www.webtoons.com
    """
    resp = _fetch(episode_url, referer=_WEBTOON_BASE)
    if not resp:
        log.error("Could not fetch viewer page: %s", episode_url)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    images: list[str] = []

    # Strategy 1: img tags with data-url (Webtoon's lazy loader)
    for img in soup.select("img._images, div#content img[data-url], .viewer_img img"):
        url = img.get("data-url") or img.get("data-src") or img.get("src") or ""
        if url and ("phinf" in url or "webtoon" in url.lower() or url.startswith("http")):
            if url not in images and not url.endswith(".gif") and "logo" not in url.lower():
                images.append(url)

    if images:
        log.info("Webtoon viewer: found %d images (strategy 1)", len(images))
        return images

    # Strategy 2: scan all <img> in the content div for hosted images
    content = soup.find(id="content") or soup.find(class_="viewer_img") or soup
    for img in content.find_all("img"):  # type: ignore[union-attr]
        for attr in ("data-url", "data-src", "src"):
            url = img.get(attr, "")
            if url and url.startswith("http") and "phinf" in url:
                if url not in images:
                    images.append(url)

    if images:
        log.info("Webtoon viewer: found %d images (strategy 2)", len(images))
        return images

    # Strategy 3: search raw HTML for CDN URLs
    cdn_re = re.compile(r'https://[a-z0-9\-]+\.pstatic\.net/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)', re.IGNORECASE)
    for url in cdn_re.findall(resp.text):
        if url not in images:
            images.append(url)

    if images:
        log.info("Webtoon viewer: found %d images (strategy 3)", len(images))
    else:
        log.warning("Webtoon viewer: no images found on %s", episode_url)

    return images


# ──────────────────────────────────────────────────────────────────────────────
# Cover art
# ──────────────────────────────────────────────────────────────────────────────

def get_series_metadata(series_url: str) -> dict:
    """
    Scrape series metadata from the Webtoon list page.

    Returns a dict with keys: summary, genres, tags, writer, publisher
    """
    resp = _fetch(parse_base_list_url(series_url))
    if not resp:
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Summary / synopsis ────────────────────────────────────────────────────
    # Try meta tags first (most stable across Webtoon redesigns), then CSS.
    summary = ""
    for sel in (
        "meta[property='og:description']",
        "meta[name='description']",
        ".summary",
        ".detail_body .summary",
        ".detail_synopsis",
        "p.summary",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get("content") or el.get_text(" ", strip=True)
            if text:
                summary = text
                break

    # ── Genre — from URL path /en/{genre}/slug/list ───────────────────────────
    genre = ""
    parsed = urlparse(series_url)
    parts  = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "en":
        genre = parts[1].replace("-", " ").title()

    # ── Author / creator ──────────────────────────────────────────────────────
    # Webtoon embeds a proprietary meta tag that is the most reliable source.
    writer = ""
    for sel in (
        "meta[property='com-linewebtoon:webtoon:author']",
        "meta[name='author']",
        ".author_area a",
        ".detail_header .author a",
        ".info_area .author",
        ".detail_header .info .author",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get("content") or el.get_text(strip=True)
            if text:
                writer = text
                break

    # ── Tags ──────────────────────────────────────────────────────────────────
    tags_els = soup.select(".genre_box a, .tag a, .genre a")
    tags = [t.get_text(strip=True) for t in tags_els if t.get_text(strip=True)]

    return {
        "summary":    summary,
        "genres":     genre,
        "tags":       ", ".join(tags),
        "writer":     writer,
        "penciller":  "",
        "publisher":  "Webtoon",
        "age_rating": "",
        "language":   "en",
    }


def get_series_cover(series_url: str, series_dir: Path) -> Optional[str]:
    """Download the series thumbnail as folder.jpg if not already present."""
    cover_path = series_dir / "folder.jpg"
    if cover_path.exists():
        return None

    resp = _fetch(parse_base_list_url(series_url))
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    # Webtoon shows a <div class="detail_header"><span class="thmb"><img ...>
    thumb = (
        soup.select_one(".detail_header .thmb img")
        or soup.select_one(".info_item .thmb img")
        or soup.select_one("meta[property='og:image']")
    )
    img_url = None
    if thumb:
        img_url = thumb.get("src") or thumb.get("content") or thumb.get("data-url")

    if not img_url:
        return None

    img_resp = _fetch(img_url)
    if not img_resp:
        return None

    try:
        cover_path.write_bytes(img_resp.content)
        log.info("Webtoon: saved folder.jpg for series")
    except Exception as exc:
        log.warning("Webtoon: could not save cover: %s", exc)

    return img_url


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def download_new_episodes(
    site_cfg: dict,
    nas_path: str,
    page_delay: float,
    chapter_delay: float,
    state: dict,
    status_callback=None,
    search_paths: Optional[list[str]] = None,
    new_manga_path: Optional[str] = None,
) -> tuple[int, str, list[float]]:
    """
    Check for and download new Webtoon episodes.

    search_paths    : all NAS paths to search for an existing series folder.
    new_manga_path  : where to place a brand-new series.

    Returns (count, series_title, [episode_nos_downloaded]).
    """
    name: str       = site_cfg.get("name", "Unknown Webtoon")
    series_url: str = site_cfg.get("url", "").strip()
    nas_folder: str = site_cfg.get("nas_folder") or name
    state_key: str  = f"_webtoon_{re.sub(r'[^a-z0-9]', '_', name.lower())}"

    if search_paths is None:
        search_paths = [nas_path]
    if new_manga_path is None:
        new_manga_path = nas_path

    def _status(msg: str, level: str = "info"):
        log.info(msg)
        if status_callback:
            status_callback(msg, level)

    if not series_url:
        log.error("Webtoon entry '%s' has no URL configured.", name)
        return 0, name, []

    _status(f"[Webtoon] {name} — checking for new episodes...", "manga")

    # Keep an existing series on whichever NAS path it already lives on;
    # route brand-new series to the configured new-manga path.
    series_dir = downloader.resolve_series_dir(nas_folder, search_paths, new_manga_path)
    series_dir.mkdir(parents=True, exist_ok=True)

    # Sync from NAS so we never re-download
    downloader.sync_state_from_nas(state, state_key, series_dir, name)
    state.setdefault(state_key, {})["title"] = name
    last_episode = downloader.get_last_chapter(state, state_key)

    # Download cover art
    get_series_cover(series_url, series_dir)

    # Write ComicInfo.xml sidecar if it doesn't exist yet
    comicinfo_path = series_dir / "ComicInfo.xml"
    if not comicinfo_path.exists():
        try:
            meta = get_series_metadata(series_url)
            ci_data = ComicInfoData(
                series     = name,
                summary    = meta.get("summary", ""),
                genres     = meta.get("genres", ""),
                tags       = meta.get("tags", ""),
                writer     = meta.get("writer", ""),
                publisher  = meta.get("publisher", "Webtoon"),
                age_rating = meta.get("age_rating", ""),
                language   = "en",
            )
            ci_mod.write_sidecar(series_dir, ci_data)
            log.info("[Webtoon] Wrote ComicInfo.xml for %s", name)
        except Exception as exc:
            log.warning("[Webtoon] Could not write ComicInfo.xml for %s: %s", name, exc)

    # Discover episodes
    all_episodes = get_episode_list(series_url)
    if not all_episodes:
        _status(f"[Webtoon] {name} — could not retrieve episode list.", "error")
        return 0, name, []

    # ── Find episodes missing from disk (gap-aware).
    # Filtering only by episode_no > last_episode would permanently skip any
    # episode that failed mid-download or was otherwise missed.
    missing_episodes = [
        ep for ep in all_episodes
        if not (series_dir / _cbz_filename(name, ep["episode_no"])).exists()
    ]

    if not missing_episodes:
        _status(f"[Webtoon] {name} — up to date (Ep.{int(all_episodes[-1]['episode_no'])})", "uptodate")
        return 0, name, []

    gap_count = len([ep for ep in missing_episodes if ep["episode_no"] <= (last_episode or 0)])
    if gap_count > 0:
        _status(
            f"[Webtoon] {name} — {len(missing_episodes)} episode(s) needed "
            f"({gap_count} gap(s) + new), downloading...",
            "manga"
        )
    else:
        _status(
            f"[Webtoon] {name} — {len(missing_episodes)} new episode(s) found, downloading...",
            "manga"
        )

    downloaded = 0
    failed_eps = 0
    downloaded_eps: list[float] = []
    highest = last_episode

    for idx, ep in enumerate(missing_episodes, 1):
        ep_no  = ep["episode_no"]
        ep_url = ep["url"]
        cbz_name = _cbz_filename(name, ep_no)
        cbz_path = series_dir / cbz_name

        if cbz_path.exists():
            if highest is None or ep_no > highest:
                highest = float(ep_no)
            continue

        _status(f"[Webtoon] {name} — Ep.{ep_no} ({idx}/{len(missing_episodes)}) downloading...", "progress")

        image_urls = get_episode_images(ep_url)
        if not image_urls:
            log.error("Webtoon: no images found for Ep.%d — skipping", ep_no)
            continue

        pages: list[bytes] = []
        failed = False

        for img_url in image_urls:
            img_resp = _fetch(img_url, referer=_WEBTOON_BASE)
            if not img_resp:
                log.error("Webtoon: image download failed: %s", img_url)
                failed = True
                break
            pages.append(img_resp.content)
            time.sleep(page_delay)

        if failed or not pages:
            _status(f"[Webtoon] {name} — Ep.{ep_no} failed, skipping.", "error")
            failed_eps += 1
            continue

        try:
            _build_cbz(pages, cbz_path)
        except Exception as exc:
            log.error("Webtoon: failed to write CBZ for Ep.%d: %s", ep_no, exc)
            if cbz_path.exists():
                cbz_path.unlink()
            continue

        _status(f"[Webtoon] {name} — Ep.{ep_no} saved ({len(pages)} images)", "saved")
        downloaded += 1
        downloaded_eps.append(float(ep_no))
        highest = float(ep_no)

        with downloader._state_lock:
            downloader.set_last_chapter(state, state_key, highest, name)
            downloader.record_chapter_download(state, state_key, float(ep_no))
            downloader.save_state(state)

        time.sleep(chapter_delay)

        # Every 10 episodes, pause for 15 seconds to avoid Webtoon rate-limiting
        # after a large batch of consecutive image downloads.
        if downloaded % 10 == 0:
            log.info("[Webtoon] %s — pausing 15s after %d episodes to avoid rate limit...", name, downloaded)
            time.sleep(15)

    if downloaded > 0:
        msg = f"[Webtoon] {name} — done! {downloaded} episode(s) saved."
        if failed_eps:
            msg += f" ({failed_eps} failed — will retry on next scan)"
        _status(msg, "done")
    elif failed_eps:
        _status(f"[Webtoon] {name} — {failed_eps} episode(s) failed (will retry next scan).", "error")

    return downloaded, name, downloaded_eps
