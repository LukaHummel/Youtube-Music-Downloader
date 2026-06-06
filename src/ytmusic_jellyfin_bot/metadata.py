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


def tag_values_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_track_metadata(metadata)
    values: dict[str, Any] = {}

    title = _text(normalized.get("track") or normalized.get("title"))
    artist = _text(normalized.get("artist") or normalized.get("albumartist") or normalized.get("creator"))
    artists = _text_list(normalized.get("artists"))
    album = _text(normalized.get("album"))
    albumartist = _text(normalized.get("albumartist") or artist)
    albumartists = _text_list(normalized.get("albumartists"))
    year = _year_from_metadata(normalized)
    track_number = _positive_int(normalized.get("track_number") or normalized.get("playlist_index"))
    track_total = _positive_int(normalized.get("track_total") or normalized.get("tracktotal"))
    lyrics = _text(normalized.get("lyrics"))
    composer = _text(normalized.get("composer"))
    composers = _text_list(normalized.get("composers"))
    artwork_url = _text(normalized.get("ytmusic_artwork_url"))

    if title:
        values["title"] = title
    if artist:
        values["artist"] = artist
    if artists:
        values["artists"] = artists
    if album:
        values["album"] = album
    if albumartist:
        values["albumartist"] = albumartist
    if albumartists:
        values["albumartists"] = albumartists
    if year:
        values["year"] = year
    if track_number:
        values["track"] = track_number
    if track_total:
        values["tracktotal"] = track_total
    if lyrics:
        values["lyrics"] = lyrics
    if composer:
        values["composer"] = composer
    if composers:
        values["composers"] = composers
    if artwork_url:
        values["artwork_url"] = artwork_url
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
    names = _text_list(value)
    return ", ".join(names) if names else None


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = _text(value)
        return [text] if text else []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = _text(item.get("name"))
        else:
            name = _text(item)
        if name:
            names.append(name)
    return names


def _year_from_metadata(metadata: Mapping[str, Any]) -> int | None:
    for key in ("year", "meta_date", "release_date", "upload_date", "date"):
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
