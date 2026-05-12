"""
MangaDex API client.

Wraps the public MangaDex v5 REST API.
Rate-limit guidance from MangaDex: no more than 5 req/s; we stay well under.
"""

import time
import logging
from typing import Optional

import requests

BASE_URL = "https://api.mangadex.org"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "MangaDownloader-Jellyfin/1.0"})

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get(path: str, params: Optional[dict] = None, retries: int = 3) -> dict:
    """GET wrapper with automatic retry on transient errors."""
    url = f"{BASE_URL}{path}"
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429:
                wait = int(exc.response.headers.get("Retry-After", 60))
                log.warning("Rate-limited by MangaDex. Waiting %ss…", wait)
                time.sleep(wait)
            elif status and 500 <= status < 600:
                log.warning("Server error %s on attempt %s/%s", status, attempt, retries)
                time.sleep(5 * attempt)
            else:
                raise
        except requests.exceptions.RequestException as exc:
            log.warning("Request error on attempt %s/%s: %s", attempt, retries, exc)
            time.sleep(5 * attempt)
    raise RuntimeError(f"Failed to GET {url} after {retries} attempts")


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_manga_info(manga_id: str) -> dict:
    """Return basic info about a manga (title, etc.)."""
    data = _get(f"/manga/{manga_id}")
    return data.get("data", {})


def get_manga_title(manga_info: dict) -> str:
    """Extract the best English (or fallback) title from a manga info object."""
    attrs = manga_info.get("attributes", {})
    titles: dict = attrs.get("title", {})
    alt_titles: list = attrs.get("altTitles", [])

    if titles.get("en"):
        return titles["en"]
    for alt in alt_titles:
        if alt.get("en"):
            return alt["en"]
    # Fall back to whatever title is first
    return next(iter(titles.values()), "Unknown Manga")


def get_chapters(
    manga_id: str,
    language: str = "en",
    after_chapter: Optional[float] = None,
) -> list[dict]:
    """
    Return all translated chapters for a manga in ascending chapter order.

    Parameters
    ----------
    manga_id      : MangaDex manga UUID
    language      : ISO 639-1 language code ("en", "fr", …)
    after_chapter : if set, only return chapters numerically greater than this
    """
    chapters: list[dict] = []
    offset = 0
    limit = 500

    while True:
        params = {
            "translatedLanguage[]": language,
            "order[chapter]": "asc",
            "order[volume]": "asc",
            "limit": limit,
            "offset": offset,
            "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
        }
        data = _get(f"/manga/{manga_id}/feed", params=params)
        results: list = data.get("data", [])
        chapters.extend(results)

        total = data.get("total", 0)
        offset += limit
        if offset >= total:
            break

        time.sleep(0.5)  # polite paging delay

    if after_chapter is not None:
        def _chap_num(ch: dict) -> float:
            raw = ch.get("attributes", {}).get("chapter") or "0"
            try:
                return float(raw)
            except ValueError:
                return 0.0

        chapters = [c for c in chapters if _chap_num(c) > after_chapter]

    return chapters


def get_chapter_pages(chapter_id: str, quality: str = "data") -> list[str]:
    """
    Return a list of full image URLs for each page of a chapter.

    Parameters
    ----------
    chapter_id : MangaDex chapter UUID
    quality    : "data" (original) or "data-saver" (compressed)
    """
    data = _get(f"/at-home/server/{chapter_id}")
    base_url: str = data["baseUrl"]
    chapter_data: dict = data["chapter"]
    hash_val: str = chapter_data["hash"]
    files: list[str] = chapter_data.get(quality, chapter_data.get("data", []))

    return [f"{base_url}/{quality}/{hash_val}/{fname}" for fname in files]


COVER_CDN = "https://uploads.mangadex.org/covers"


def get_cover_url(manga_id: str, quality: str = "512") -> Optional[str]:
    """
    Return the URL of the primary cover image for a manga.

    Parameters
    ----------
    manga_id : MangaDex manga UUID
    quality  : "256", "512", or "" (original full resolution)
    """
    params = {
        "manga[]": manga_id,
        "order[volume]": "desc",
        "limit": 1,
    }
    try:
        data = _get("/cover", params=params)
    except Exception as exc:
        log.warning("Could not fetch cover for %s: %s", manga_id, exc)
        return None

    covers = data.get("data", [])
    if not covers:
        return None

    filename: str = covers[0].get("attributes", {}).get("fileName", "")
    if not filename:
        return None

    suffix = f".{quality}.jpg" if quality else ""
    return f"{COVER_CDN}/{manga_id}/{filename}{suffix}"


def browse_manga(
    query: str = "",
    sort: str = "popular",
    offset: int = 0,
    limit: int = 24,
) -> dict:
    """
    Browse / search MangaDex with sorting and pagination.

    sort options: "popular", "latest", "az", "new"
    Returns {"results": [...], "total": int, "offset": int}
    """
    order_map = {
        "popular": {"order[followedCount]": "desc"},
        "latest":  {"order[latestUploadedChapter]": "desc"},
        "az":      {"order[title]": "asc"},
        "new":     {"order[createdAt]": "desc"},
    }
    params = {
        "limit":  limit,
        "offset": offset,
        "includes[]": ["cover_art"],
        "contentRating[]": ["safe", "suggestive"],
        **order_map.get(sort, order_map["popular"]),
    }
    if query.strip():
        params["title"] = query.strip()

    try:
        data = _get("/manga", params=params)
    except Exception as exc:
        log.warning("browse_manga failed: %s", exc)
        return {"results": [], "total": 0, "offset": offset}

    results = []
    for manga in data.get("data", []):
        manga_id = manga["id"]
        title    = get_manga_title(manga)
        attrs    = manga.get("attributes", {})

        desc_map    = attrs.get("description", {})
        description = desc_map.get("en", "") or next(iter(desc_map.values()), "")
        if len(description) > 160:
            description = description[:157] + "..."

        status = attrs.get("status", "").capitalize()
        tags   = [
            t["attributes"]["name"].get("en", "")
            for t in attrs.get("tags", [])
            if t["attributes"]["name"].get("en")
        ][:4]

        cover_url = None
        for rel in manga.get("relationships", []):
            if rel["type"] == "cover_art":
                fname = rel.get("attributes", {}).get("fileName", "")
                if fname:
                    cover_url = f"{COVER_CDN}/{manga_id}/{fname}.256.jpg"
                break

        results.append({
            "id":          manga_id,
            "title":       title,
            "description": description,
            "status":      status,
            "tags":        tags,
            "cover_url":   cover_url,
        })

    return {
        "results": results,
        "total":   data.get("total", len(results)),
        "offset":  offset,
    }


def search_manga(query: str, limit: int = 12) -> list[dict]:
    """
    Search MangaDex by title and return a list of results with id, title,
    description, and a thumbnail cover URL.
    """
    params = {
        "title": query,
        "limit": limit,
        "includes[]": ["cover_art"],
        "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
        "order[relevance]": "desc",
    }
    try:
        data = _get("/manga", params=params)
    except Exception as exc:
        log.warning("Search failed for %r: %s", query, exc)
        return []

    results = []
    for manga in data.get("data", []):
        manga_id = manga["id"]
        title    = get_manga_title(manga)
        attrs    = manga.get("attributes", {})

        # Description (English preferred)
        desc_map = attrs.get("description", {})
        description = desc_map.get("en", "") or next(iter(desc_map.values()), "")
        if len(description) > 200:
            description = description[:197] + "..."

        # Thumbnail from embedded cover_art relationship
        cover_url = None
        for rel in manga.get("relationships", []):
            if rel["type"] == "cover_art":
                fname = rel.get("attributes", {}).get("fileName", "")
                if fname:
                    cover_url = f"{COVER_CDN}/{manga_id}/{fname}.256.jpg"
                break

        results.append({
            "id":          manga_id,
            "title":       title,
            "description": description,
            "cover_url":   cover_url,
        })

    return results


def download_image(url: str, retries: int = 3) -> bytes:
    """Download a single image and return its raw bytes."""
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, timeout=60)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.RequestException as exc:
            log.warning("Image download error attempt %s/%s: %s", attempt, retries, exc)
            time.sleep(3 * attempt)
    raise RuntimeError(f"Failed to download image: {url}")
