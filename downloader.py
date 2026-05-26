"""
Core download logic.

Responsibilities:
  - Load / save the state file that tracks downloaded chapters.
  - For each manga, fetch new English chapters from MangaDex.
  - Download every page and package them into a .cbz file.
  - Save .cbz files in a Jellyfin-compatible folder layout on the NAS.

Jellyfin folder layout produced:
  <nas_path>/
    <Series Name>/
      <Series Name> - Chapter 001.cbz
      <Series Name> - Chapter 001.5.cbz
      <Series Name> - Chapter 002.cbz
      …
"""

import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# Protects all state-dict mutations and file writes across concurrent workers.
_state_lock = threading.RLock()


import mangadex_api as api

log = logging.getLogger(__name__)

from paths import STATE_FILE

# Matches chapter numbers in CBZ filenames regardless of naming convention:
#   c1181.cbz | Chapter 1181.cbz | Series - Chapter 001.cbz | chapter-1181.cbz
_CHAP_NUM_RE = re.compile(
    r"(?:^c|[Cc]hapter[-\s]*)(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Return the persisted state dict, or an empty one if none exists yet.

    If state.json is corrupted (truncated write, disk error, etc.) the bad
    file is backed up to state.json.bak and an empty dict is returned so the
    server keeps running rather than crashing on every request.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (json.JSONDecodeError, ValueError) as exc:
        log.error(
            "state.json is corrupted (%s) — backing up to state.json.bak and starting fresh.",
            exc,
        )
        import shutil
        backup = STATE_FILE.with_suffix(".json.bak")
        try:
            shutil.copy2(STATE_FILE, backup)
        except Exception:
            pass
        return {}


def save_state(state: dict) -> None:
    """Write state atomically — write to a temp file then rename so a crash
    mid-write never leaves a half-written (corrupt) state.json.
    The global _state_lock must be held by the caller."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    tmp.replace(STATE_FILE)


def get_last_chapter(state: dict, manga_id: str) -> Optional[float]:
    """Return the chapter number of the last successfully downloaded chapter."""
    entry = state.get(manga_id, {})
    val = entry.get("last_chapter")
    return float(val) if val is not None else None


def set_last_chapter(state: dict, manga_id: str, chapter_num: float, title: str) -> None:
    """Update last_chapter in state. Caller must hold _state_lock."""
    if manga_id not in state:
        state[manga_id] = {}
    state[manga_id]["last_chapter"] = chapter_num
    state[manga_id]["title"] = title


def record_chapter_download(state: dict, manga_id: str, chapter_num: float) -> None:
    """Stamp a chapter as downloaded at the current time. Caller must hold _state_lock."""
    state.setdefault(manga_id, {}).setdefault("chapters", {})[str(chapter_num)] = {
        "downloaded_at": datetime.now().isoformat(timespec="seconds")
    }


# ──────────────────────────────────────────────────────────────────────────────
# NAS scanning — detect already-present chapters to prevent duplicates
# ──────────────────────────────────────────────────────────────────────────────

def scan_nas_for_chapters(series_dir: Path) -> set:
    """
    Scan a series folder for existing CBZ files and extract chapter numbers.

    Recognises many naming patterns:
      c1181.cbz
      Chapter 1181.cbz
      One Piece - Chapter 001.cbz
      chapter-1181.cbz

    Returns a set of float chapter numbers already present on the NAS.
    """
    found: set = set()
    if not series_dir.exists():
        return found

    for cbz in series_dir.glob("*.cbz"):
        stem = cbz.stem
        match = _CHAP_NUM_RE.search(stem)
        if match:
            try:
                found.add(float(match.group(1)))
            except ValueError:
                pass
    return found


def sync_state_from_nas(state: dict, manga_id: str, series_dir: Path, title: str) -> None:
    """
    Scan the NAS folder and update state so last_chapter reflects what's
    actually present on disk. Prevents re-downloading already-present chapters
    even if state.json was reset or chapters were added manually.
    """
    nas_chapters = scan_nas_for_chapters(series_dir)
    if not nas_chapters:
        return

    highest = max(nas_chapters)
    state_last = get_last_chapter(state, manga_id)

    if state_last is None or highest > state_last:
        log.info(
            "  [NAS] Found %d existing chapters on disk; highest=%.4g. Updating state.",
            len(nas_chapters),
            highest,
        )
        with _state_lock:
            set_last_chapter(state, manga_id, highest, title)
            chapters_map = state.setdefault(manga_id, {}).setdefault("chapters", {})
            for num in nas_chapters:
                if str(num) not in chapters_map:
                    chapters_map[str(num)] = {"downloaded_at": "pre-existing"}
            save_state(state)


# ──────────────────────────────────────────────────────────────────────────────
# File-system helpers
# ──────────────────────────────────────────────────────────────────────────────

_INVALID_PATH_CHARS = re.compile(r'[\\/*?:"<>|]')


def _safe_name(name: str) -> str:
    """Strip characters that are invalid in Windows/Linux file paths."""
    return _INVALID_PATH_CHARS.sub("_", name).strip()


def _chapter_number(chapter: dict) -> float:
    raw = chapter.get("attributes", {}).get("chapter") or "0"
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _cbz_filename(series_name: str, chapter: dict) -> str:
    """
    Produce a sortable filename like:
      My Hero Academia - Chapter 001.cbz
      My Hero Academia - Chapter 001.5.cbz
    """
    num = _chapter_number(chapter)
    # Format: 3 digits before decimal, keep .5 etc.
    if num == int(num):
        num_str = f"{int(num):03d}"
    else:
        whole = int(num)
        frac = round(num - whole, 3)
        frac_str = f"{frac:.3f}"[1:]  # e.g. ".5"
        num_str = f"{whole:03d}{frac_str}"
    return f"{_safe_name(series_name)} - Chapter {num_str}.cbz"


# ──────────────────────────────────────────────────────────────────────────────
# Cover art
# ──────────────────────────────────────────────────────────────────────────────

def _download_cover_art(manga_id: str, series_dir: Path) -> Optional[str]:
    """
    Download the primary cover image and save it as folder.jpg in the series dir.
    Jellyfin reads folder.jpg as the series poster automatically.
    Skips the download if folder.jpg already exists.

    Returns the MangaDex CDN URL for the cover (useful as a fallback when the
    NAS is not reachable), or None if no cover was found.
    """
    cover_url = api.get_cover_url(manga_id, quality="512")
    if not cover_url:
        log.warning("  No cover art found for manga %s", manga_id)
        return None

    cover_path = series_dir / "folder.jpg"
    if not cover_path.exists():
        try:
            img_bytes = api.download_image(cover_url)
            with cover_path.open("wb") as fh:
                fh.write(img_bytes)
            log.info("  [COVER] Saved folder.jpg (%d KB)", len(img_bytes) // 1024)
        except Exception as exc:
            log.warning("  Could not download cover art: %s", exc)

    return cover_url


# ──────────────────────────────────────────────────────────────────────────────
# CBZ creation
# ──────────────────────────────────────────────────────────────────────────────

def _build_cbz(pages: list[bytes], cbz_path: Path) -> None:
    """Write pages (raw image bytes) into a CBZ (ZIP) archive."""
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cbz_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        for idx, page_bytes in enumerate(pages, start=1):
            # Detect extension from magic bytes
            ext = _detect_image_ext(page_bytes)
            zf.writestr(f"{idx:03d}{ext}", page_bytes)
    tmp_path.rename(cbz_path)


def _detect_image_ext(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:2] in (b"\xff\xd8",):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


# ──────────────────────────────────────────────────────────────────────────────
# Main per-manga download routine
# ──────────────────────────────────────────────────────────────────────────────

def download_manga(
    manga_id: str,
    config_name: Optional[str],
    nas_path: str,
    language: str,
    image_quality: str,
    page_delay: float,
    chapter_delay: float,
    max_chapters: int,
    state: dict,
    status_callback=None,
) -> tuple[int, str, list[float]]:
    """
    Check for and download new chapters for one manga.

    Returns (count, series_title, [chapter_nums_downloaded]).
    """
    def _status(msg: str, level: str = "info"):
        log.info(msg)
        if status_callback:
            status_callback(msg, level)

    # Fetch manga metadata
    try:
        manga_info = api.get_manga_info(manga_id)
    except Exception as exc:
        log.error("Could not fetch manga info for %s: %s", manga_id, exc)
        return 0, "", []

    api_title = api.get_manga_title(manga_info)
    series_name = config_name or api_title

    _status(f"[MangaDex] {series_name} — checking for new chapters...", "manga")

    series_dir = Path(nas_path) / _safe_name(series_name)
    series_dir.mkdir(parents=True, exist_ok=True)

    # Sync state from NAS so we never re-download chapters already on disk
    sync_state_from_nas(state, manga_id, series_dir, series_name)
    highest_chapter = get_last_chapter(state, manga_id)

    # Skip the MangaDex cover API call if we already have the URL cached and
    # folder.jpg is already on disk — saves one extra round-trip per manga per scan.
    cached_cover = state.get(manga_id, {}).get("cover_url")
    cover_path   = series_dir / "folder.jpg"
    if cached_cover and cover_path.exists():
        cover_url = cached_cover
    else:
        cover_url = _download_cover_art(manga_id, series_dir)
        if cover_url:
            with _state_lock:
                state.setdefault(manga_id, {})["cover_url"] = cover_url
                save_state(state)

    downloaded = 0
    downloaded_chapters: list[float] = []
    batch_size = max_chapters if max_chapters else None

    while True:
        try:
            chapters = api.get_chapters(manga_id, language=language, after_chapter=highest_chapter)
        except Exception as exc:
            log.error("Could not fetch chapters for %s: %s", manga_id, exc)
            break

        if not chapters:
            if downloaded == 0:
                _status(f"[MangaDex] {series_name} — up to date, no new chapters.", "uptodate")
            else:
                _status(f"[MangaDex] {series_name} — done! {downloaded} chapter(s) saved.", "done")
            break

        # Guard: if this is a brand-new series (nothing downloaded yet) and the
        # earliest available chapter is not chapter 1, don't start mid-series.
        # This prevents grabbing e.g. only chapter 36 when chapters 1-35 are
        # unavailable/blocked on MangaDex.
        if highest_chapter is None:
            min_chap = min(_chapter_number(c) for c in chapters)
            if min_chap > 1:
                _status(
                    f"[MangaDex] {series_name} — skipping: earliest available chapter is "
                    f"Ch.{min_chap:.4g}, not Ch.1. Will not start mid-series.",
                    "skip",
                )
                break

        total_remaining = len(chapters)
        batch = chapters[:batch_size] if batch_size else chapters
        _status(
            f"[MangaDex] {series_name} — {total_remaining} new chapter(s) found, downloading...",
            "manga"
        )

        for idx, chapter in enumerate(batch, 1):
            chap_num = _chapter_number(chapter)
            chap_id = chapter["id"]
            cbz_name = _cbz_filename(series_name, chapter)
            cbz_path = series_dir / cbz_name

            if cbz_path.exists():
                if highest_chapter is None or chap_num > highest_chapter:
                    highest_chapter = chap_num
                continue

            _status(
                f"[MangaDex] {series_name} — Ch.{chap_num:.4g} ({idx}/{len(batch)}) downloading...",
                "progress"
            )

            # Get page URLs
            try:
                page_urls = api.get_chapter_pages(chap_id, quality=image_quality)
            except Exception as exc:
                log.error("Could not get page URLs for chapter %s: %s", chap_id, exc)
                continue

            if not page_urls:
                log.warning("Chapter %s has no pages - skipping", chap_id)
                continue

            # Download pages
            pages: list[bytes] = []
            failed = False
            for url in page_urls:
                try:
                    img = api.download_image(url)
                    pages.append(img)
                    time.sleep(page_delay)
                except Exception as exc:
                    log.error("Page download failed (%s): %s", url, exc)
                    failed = True
                    break

            if failed:
                _status(f"[MangaDex] {series_name} — Ch.{chap_num:.4g} failed, skipping.", "error")
                continue

            # Package into CBZ
            try:
                _build_cbz(pages, cbz_path)
            except Exception as exc:
                log.error("Failed to write CBZ: %s", exc)
                if cbz_path.exists():
                    cbz_path.unlink()
                continue

            _status(
                f"[MangaDex] {series_name} — Ch.{chap_num:.4g} saved ({len(pages)} pages)",
                "saved"
            )
            downloaded += 1
            downloaded_chapters.append(chap_num)

            if highest_chapter is None or chap_num > highest_chapter:
                highest_chapter = chap_num

            if highest_chapter is not None:
                with _state_lock:
                    set_last_chapter(state, manga_id, highest_chapter, series_name)
                    record_chapter_download(state, manga_id, chap_num)
                    save_state(state)

            time.sleep(chapter_delay)

        if not batch_size or total_remaining <= batch_size:
            if downloaded > 0:
                _status(f"[MangaDex] {series_name} — done! {downloaded} chapter(s) saved.", "done")
            break

    return downloaded, series_name, downloaded_chapters
