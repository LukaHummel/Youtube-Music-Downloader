from __future__ import annotations

import re
from typing import Any, Mapping

TITLE_SEPARATORS = (" - ", " – ", " — ")
TRAILING_PIPE_RE = re.compile(r"\s+\|\s+.*$")
NOISE_BRACKET_RE = re.compile(
    r"\s*[\(\[][^\)\]]*"
    r"(official|lyrics?|audio|video|visualizer|music video|hd|4k|sped up|slowed|nightcore|bass boosted)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(\d{4})")


def normalize_track_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata)
    raw_title = _text(normalized.get("title") or normalized.get("track"))
    track = _text(normalized.get("track"))
    artist = _text(normalized.get("artist") or normalized.get("albumartist"))

    if not artist:
        artist = _artists_list_text(normalized.get("artists"))

    if raw_title and (not artist or not track):
        parsed = parse_artist_title(raw_title)
        if parsed:
            parsed_artist, parsed_track = parsed
            artist = artist or parsed_artist
            track = track or parsed_track

    if raw_title and not track:
        track = clean_track_title(raw_title)

    if not artist:
        artist = _text(normalized.get("creator") or normalized.get("channel"))

    if track:
        normalized["track"] = track
    if artist:
        normalized["artist"] = artist
        normalized.setdefault("albumartist", artist)
    return normalized


def tag_values_from_metadata(metadata: Mapping[str, Any]) -> dict[str, str | int]:
    normalized = normalize_track_metadata(metadata)
    values: dict[str, str | int] = {}

    title = _text(normalized.get("track") or normalized.get("title"))
    artist = _text(normalized.get("artist") or normalized.get("albumartist") or normalized.get("creator"))
    album = _text(normalized.get("album"))
    albumartist = _text(normalized.get("albumartist") or artist)
    year = _year_from_metadata(normalized)
    track_number = _positive_int(normalized.get("track_number") or normalized.get("playlist_index"))

    if title:
        values["title"] = title
    if artist:
        values["artist"] = artist
    if album:
        values["album"] = album
    if albumartist:
        values["albumartist"] = albumartist
    if year:
        values["year"] = year
    if track_number:
        values["track"] = track_number
    return values


def parse_artist_title(title: str) -> tuple[str, str] | None:
    for separator in TITLE_SEPARATORS:
        if separator not in title:
            continue
        artist, track = title.split(separator, 1)
        artist = _text(artist)
        track = clean_track_title(track)
        if artist and track:
            return artist, track
    return None


def clean_track_title(title: str) -> str:
    cleaned = TRAILING_PIPE_RE.sub("", title).strip()
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = NOISE_BRACKET_RE.sub("", cleaned).strip()
    return re.sub(r"\s+", " ", cleaned).strip(" \"'")


def _artists_list_text(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for artist in value:
        if isinstance(artist, dict):
            name = _text(artist.get("name"))
            if name:
                names.append(name)
        else:
            name = _text(artist)
            if name:
                names.append(name)
    return ", ".join(names) if names else None


def _year_from_metadata(metadata: Mapping[str, Any]) -> int | None:
    for key in ("meta_date", "release_date", "upload_date", "date"):
        value = _text(metadata.get(key))
        if not value:
            continue
        match = YEAR_RE.search(value)
        if match:
            return _positive_int(match.group(1))
    return None


def _positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
