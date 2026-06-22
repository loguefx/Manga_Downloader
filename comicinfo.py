"""
ComicInfo.xml builder.

ComicInfo.xml is the standard metadata file embedded inside CBZ archives.
Mangrove (and Komga, Kavita, etc.) reads this file during library scans to
populate the database with summary, genres, tags, author, and age rating.

Reference: https://anansi-project.github.io/docs/comicinfo/schemas/v2.0
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ComicInfoData:
    series: str = ""
    chapter_number: Optional[float] = None
    chapter_title: str = ""
    summary: str = ""
    writer: str = ""
    penciller: str = ""
    publisher: str = ""
    genres: str = ""          # comma-separated
    tags: str = ""            # comma-separated
    language: str = "en"
    age_rating: str = ""      # e.g. "Everyone", "Teen", "Mature 17+"
    count: Optional[int] = None   # total chapters if known


# MangaDex contentRating → ComicInfo AgeRating
_AGE_RATING_MAP = {
    "safe":         "Everyone",
    "suggestive":   "Teen",
    "erotica":      "Mature 17+",
    "pornographic": "Adults Only 18+",
}


def build_xml(info: ComicInfoData) -> bytes:
    """Serialise a ComicInfoData to UTF-8 XML bytes ready to embed in a CBZ."""
    root = ET.Element("ComicInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

    def _add(tag: str, value):
        if value is not None and str(value).strip():
            el = ET.SubElement(root, tag)
            el.text = str(value).strip()

    _add("Series",      info.series)
    if info.chapter_number is not None:
        # ComicInfo Number is a float; use int format when possible
        num = int(info.chapter_number) if info.chapter_number == int(info.chapter_number) else info.chapter_number
        _add("Number", num)
    _add("Title",       info.chapter_title)
    _add("Summary",     info.summary)
    _add("Writer",      info.writer)
    _add("Penciller",   info.penciller)
    _add("Publisher",   info.publisher)
    _add("Genre",       info.genres)
    _add("Tags",        info.tags)
    _add("LanguageISO", info.language)
    _add("AgeRating",   info.age_rating)
    if info.count is not None:
        _add("Count", info.count)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    # Serialise to a unicode string then encode — writing encoding="unicode"
    # directly into a BytesIO raises "a bytes-like object is required, not 'str'".
    xml_body = ET.tostring(root, encoding="unicode")
    return ("<?xml version='1.0' encoding='utf-8'?>\n" + xml_body).encode("utf-8")


def write_sidecar(series_dir, info: ComicInfoData, overwrite: bool = False) -> bool:
    """
    Write ComicInfo.xml as a sidecar file in the manga series folder,
    alongside folder.jpg and the CBZ files.

    Returns True if the file was written, False if it already existed
    (and overwrite=False).

      series_dir/
        folder.jpg
        ComicInfo.xml   ← written here
        Series - Chapter 001.cbz
        Series - Chapter 002.cbz
        ...
    """
    from pathlib import Path
    path = Path(series_dir) / "ComicInfo.xml"
    if path.exists() and not overwrite:
        return False
    path.write_bytes(build_xml(info))
    return True
