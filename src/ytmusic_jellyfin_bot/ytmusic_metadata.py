from __future__ import annotations

import asyncio
import functools
import logging
import re
from collections.abc import Mapping
from typing import Any, Callable

import requests

from .config import AppConfig
from .metadata import clean_track_title, normalize_track_metadata, parse_artist_title
from .models import PreflightItem, PreflightResult
from .ytmusic_auth import YtMusicAuthManager

LOGGER = logging.getLogger(__name__)

SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "auth",
    "cookie",
    "cookies",
    "feedbackToken",
    "feedbackTokens",
    "refresh_token",
    "streamingData",
    "url",
}

LOGGED_METADATA_FIELDS = (
    "track",
    "title",
    "artist",
    "artists",
    "album",
    "albumartist",
    "albumartists",
    "year",
    "track_number",
    "track_total",
    "tracktotal",
    "lyrics",
    "lyrics_source",
    "composer",
    "composers",
    "ytmusic_video_id",
    "ytmusic_album_id",
    "ytmusic_playlist_id",
    "ytmusic_lyrics_id",
    "ytmusic_related_id",
    "ytmusic_counterpart_video_id",
    "ytmusic_video_type",
    "ytmusic_is_explicit",
    "ytmusic_artwork_url",
    "ytmusic_artwork_width",
    "ytmusic_artwork_height",
    "ytmusic_credits",
)

MAX_LOG_VALUE_LENGTH = 100
VIDEO_TYPES_WITH_SPARSE_WATCH_METADATA = {"MUSIC_VIDEO_TYPE_OMV", "MUSIC_VIDEO_TYPE_UGC"}
MATCH_TEXT_RE = re.compile(r"[^a-z0-9]+")
TITLE_SUFFIX_NOISE_RE = re.compile(
    r"\s+[-–—]\s+(?:official\s+)?(?:music\s+)?(?:video|visuali[sz]er|audio|lyrics?|lyric\s+video).*$",
    re.IGNORECASE,
)


class YtMusicMetadataProvider:
    def __init__(
        self,
        config: AppConfig,
        auth_manager: YtMusicAuthManager,
        *,
        client_factory: Callable[..., Any] | None = None,
        oauth_credentials_factory: Callable[..., Any] | None = None,
    ):
        self.config = config
        self.auth_manager = auth_manager
        self._client_factory = client_factory
        self._oauth_credentials_factory = oauth_credentials_factory
        self._client: Any | None = None
        self._client_authenticated = False
        self._album_cache: dict[str, dict[str, Any] | None] = {}
        self._credits_cache: dict[str, dict[str, Any] | None] = {}
        self._lyrics_cache: dict[str, dict[str, Any] | None] = {}
        self._search_cache: dict[str, list[dict[str, Any]]] = {}

    def clear_client_cache(self) -> None:
        self._client = None
        self._client_authenticated = False
        self._album_cache.clear()
        self._credits_cache.clear()
        self._lyrics_cache.clear()
        self._search_cache.clear()

    async def enrich_preflight(self, preflight: PreflightResult) -> PreflightResult:
        if not self.config.ytmusic_metadata_enabled:
            LOGGER.info(
                "ytmusic metadata enrichment skipped: enabled=False source_id=%s items=%s",
                preflight.source_id,
                len(preflight.items),
            )
            return preflight
        LOGGER.info(
            "ytmusic metadata enrichment started: source_id=%s source_title=%s items=%s",
            preflight.source_id,
            _log_value(preflight.source_title),
            len(preflight.items),
        )
        items: list[PreflightItem] = []
        changed_items = 0
        all_changed_fields: set[str] = set()
        all_added_fields: set[str] = set()
        for item in preflight.items:
            enriched_item = await self.enrich_item(item)
            changed_fields = _metadata_changed_fields(item.metadata, enriched_item.metadata)
            added_fields = _metadata_added_fields(item.metadata, enriched_item.metadata)
            if changed_fields:
                changed_items += 1
                all_changed_fields.update(changed_fields)
                all_added_fields.update(added_fields)
            items.append(enriched_item)
        LOGGER.info(
            "ytmusic metadata enrichment completed: source_id=%s items=%s changed_items=%s unchanged_items=%s "
            "changed_fields=%s added_fields=%s",
            preflight.source_id,
            len(items),
            changed_items,
            len(items) - changed_items,
            _field_list(all_changed_fields),
            _field_list(all_added_fields),
        )
        return PreflightResult(
            source_id=preflight.source_id,
            source_title=preflight.source_title,
            playlist_title=preflight.playlist_title,
            items=items,
        )

    async def enrich_item(self, item: PreflightItem) -> PreflightItem:
        if not self.config.ytmusic_metadata_enabled or not item.youtube_video_id:
            return item
        client = self._client_or_none()
        if client is None:
            return item

        try:
            return await self._enrich_item_with_client(item, client)
        except Exception as exc:
            if self._client_authenticated:
                LOGGER.warning(
                    "Authenticated ytmusic metadata lookup failed for video_id=%s; retrying anonymously: %s",
                    item.youtube_video_id,
                    exc,
                )
                anonymous_client = self._anonymous_client_or_none()
                if anonymous_client is not None:
                    try:
                        return await self._enrich_item_with_client(item, anonymous_client)
                    except Exception as anonymous_exc:
                        LOGGER.warning(
                            "Anonymous ytmusic metadata retry failed for video_id=%s: %s",
                            item.youtube_video_id,
                            anonymous_exc,
                        )
                        return item
            else:
                authenticated_client = self._authenticated_client_or_none()
                if authenticated_client is not None:
                    LOGGER.warning(
                        "Anonymous ytmusic metadata lookup failed for video_id=%s; retrying authenticated: %s",
                        item.youtube_video_id,
                        exc,
                    )
                    try:
                        return await self._enrich_item_with_client(item, authenticated_client)
                    except Exception as authenticated_exc:
                        LOGGER.warning(
                            "Authenticated ytmusic metadata retry failed for video_id=%s: %s",
                            item.youtube_video_id,
                            authenticated_exc,
                        )
                        return item
            LOGGER.warning("ytmusic metadata enrichment failed for video_id=%s: %s", item.youtube_video_id, exc)
            return item

    async def _enrich_item_with_client(self, item: PreflightItem, client: Any) -> PreflightItem:
        watch = await asyncio.to_thread(client.get_watch_playlist, videoId=item.youtube_video_id, limit=1)
        if not isinstance(watch, dict):
            LOGGER.info(
                "ytmusic metadata result: video_id=%s client=%s matched=False reason=invalid_watch_response",
                item.youtube_video_id,
                _client_label(self._client_authenticated),
            )
            return item
        track = _select_watch_track(watch, item.youtube_video_id)
        if not track:
            LOGGER.info(
                "ytmusic metadata result: video_id=%s client=%s matched=False reason=no_watch_tracks",
                item.youtube_video_id,
                _client_label(self._client_authenticated),
            )
            return item
        lookup_video_id = item.youtube_video_id
        fallback = await self._song_search_fallback(client, item=item, watch=watch, track=track)
        fallback_used = fallback is not None
        if fallback:
            watch, track, lookup_video_id = fallback

        enriched = dict(item.metadata)
        enriched.update(_metadata_from_watch(track, watch, lookup_video_id))
        if fallback_used and lookup_video_id != item.youtube_video_id:
            enriched.setdefault("ytmusic_counterpart_video_id", item.youtube_video_id)

        album_id = _album_id(track)
        album = await self._get_album(client, album_id)
        if album:
            enriched.update(_metadata_from_album(album, lookup_video_id))

        credits_browse_id = _text(enriched.get("ytmusic_credits_browse_id"))
        if self.config.ytmusic_fetch_credits and credits_browse_id:
            credits = await self._get_credits(client, credits_browse_id)
            if credits:
                enriched.update(_metadata_from_credits(credits))

        lyrics_id = _text(enriched.get("ytmusic_lyrics_id"))
        if self.config.ytmusic_fetch_lyrics and lyrics_id:
            lyrics = await self._get_lyrics(client, lyrics_id)
            if lyrics:
                enriched.update(_metadata_from_lyrics(lyrics))

        if self.config.ytmusic_embed_artwork:
            artwork = _select_artwork(enriched)
            if artwork:
                enriched.update(artwork)
        else:
            _drop_internal_metadata(enriched)

        _drop_internal_metadata(enriched)
        enriched = normalize_track_metadata(enriched)
        changed_fields = _metadata_changed_fields(item.metadata, enriched)
        added_fields = _metadata_added_fields(item.metadata, enriched)
        LOGGER.info(
            "ytmusic metadata result: video_id=%s client=%s matched=True matched_video_id=%s "
            "changed_fields=%s added_fields=%s title=%s artist=%s album=%s albumartist=%s year=%s "
            "track_number=%s track_total=%s album_id=%s playlist_id=%s lyrics=%s credits=%s artwork=%s "
            "artwork_size=%sx%s fallback=%s missing_rich_fields=%s",
            item.youtube_video_id,
            _client_label(self._client_authenticated),
            _log_value(enriched.get("ytmusic_video_id")),
            _field_list(changed_fields),
            _field_list(added_fields),
            _log_value(enriched.get("track") or enriched.get("title")),
            _log_value(enriched.get("artist")),
            _log_value(enriched.get("album")),
            _log_value(enriched.get("albumartist")),
            _log_value(enriched.get("year")),
            _log_value(enriched.get("track_number")),
            _log_value(enriched.get("track_total")),
            _log_value(enriched.get("ytmusic_album_id")),
            _log_value(enriched.get("ytmusic_playlist_id")),
            bool(enriched.get("lyrics")),
            bool(enriched.get("ytmusic_credits")),
            bool(enriched.get("ytmusic_artwork_url")),
            enriched.get("ytmusic_artwork_width") or 0,
            enriched.get("ytmusic_artwork_height") or 0,
            "song_search" if fallback_used else "none",
            _field_list(_missing_rich_fields(enriched)),
        )
        return PreflightItem(
            item_index=item.item_index,
            source_url=item.source_url,
            normalized_url=item.normalized_url,
            youtube_video_id=item.youtube_video_id,
            playlist_item_id=item.playlist_item_id,
            title=enriched.get("track") or enriched.get("title"),
            artist=enriched.get("artist") or enriched.get("albumartist"),
            album=enriched.get("album"),
            metadata=enriched,
        )

    async def _song_search_fallback(
        self,
        client: Any,
        *,
        item: PreflightItem,
        watch: dict[str, Any],
        track: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str] | None:
        direct_metadata = _metadata_from_watch(track, watch, item.youtube_video_id)
        if not _needs_song_search_fallback(direct_metadata):
            return None
        query = _song_search_query(item, track)
        if not query:
            return None

        results = await self._search_songs(client, query)
        candidate = _select_search_song(results, item=item, track=track)
        candidate_video_id = _text(candidate.get("videoId")) if candidate else None
        if not (candidate and candidate_video_id) or candidate_video_id == item.youtube_video_id:
            LOGGER.info(
                "ytmusic song search fallback not accepted: video_id=%s query=%s result_count=%s "
                "reason=no_exact_song_match",
                item.youtube_video_id,
                _log_value(query),
                len(results),
            )
            return None

        try:
            candidate_watch = await asyncio.to_thread(client.get_watch_playlist, videoId=candidate_video_id, limit=1)
        except Exception as exc:
            LOGGER.warning(
                "ytmusic song search fallback watch lookup failed: video_id=%s matched_video_id=%s error=%s",
                item.youtube_video_id,
                candidate_video_id,
                exc,
            )
            return None
        if not isinstance(candidate_watch, dict):
            return None

        candidate_track = _select_watch_track(candidate_watch, candidate_video_id) or candidate
        candidate_track = _merge_missing_search_result_fields(candidate_track, candidate)
        LOGGER.info(
            "ytmusic song search fallback accepted: video_id=%s query=%s matched_video_id=%s title=%s artist=%s "
            "album=%s",
            item.youtube_video_id,
            _log_value(query),
            candidate_video_id,
            _log_value(candidate_track.get("title")),
            _log_value(", ".join(_artist_names(candidate_track.get("artists")))),
            _log_value(_album_name(candidate_track)),
        )
        return candidate_watch, candidate_track, candidate_video_id

    async def _search_songs(self, client: Any, query: str) -> list[dict[str, Any]]:
        cache_key = query.casefold()
        if cache_key not in self._search_cache:
            try:
                results = await asyncio.to_thread(client.search, query, filter="songs", limit=10)
            except Exception as exc:
                LOGGER.warning("ytmusic song search fallback failed: query=%s error=%s", _log_value(query), exc)
                results = []
            self._search_cache[cache_key] = [result for result in results if isinstance(result, dict)]
        return self._search_cache[cache_key]

    async def _get_album(self, client: Any, album_id: str | None) -> dict[str, Any] | None:
        if not album_id:
            return None
        if album_id not in self._album_cache:
            try:
                album = await asyncio.to_thread(client.get_album, album_id)
            except Exception as exc:
                LOGGER.warning("ytmusic album lookup failed album_id=%s: %s", album_id, exc)
                album = None
            self._album_cache[album_id] = album if isinstance(album, dict) else None
        return self._album_cache[album_id]

    async def _get_credits(self, client: Any, credits_browse_id: str) -> dict[str, Any] | None:
        if credits_browse_id not in self._credits_cache:
            try:
                credits = await asyncio.to_thread(client.get_song_credits, credits_browse_id)
            except Exception as exc:
                LOGGER.warning("ytmusic credits lookup failed browse_id=%s: %s", credits_browse_id, exc)
                credits = None
            self._credits_cache[credits_browse_id] = credits if isinstance(credits, dict) else None
        return self._credits_cache[credits_browse_id]

    async def _get_lyrics(self, client: Any, lyrics_id: str) -> dict[str, Any] | None:
        if lyrics_id not in self._lyrics_cache:
            try:
                lyrics = await asyncio.to_thread(client.get_lyrics, lyrics_id, timestamps=False)
            except Exception as exc:
                LOGGER.warning("ytmusic lyrics lookup failed browse_id=%s: %s", lyrics_id, exc)
                lyrics = None
            self._lyrics_cache[lyrics_id] = _mapping_from_object(lyrics)
        return self._lyrics_cache[lyrics_id]

    def _client_or_none(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            self._client = self._create_client(authenticated=False)
            self._client_authenticated = False
            return self._client
        except Exception as fallback_exc:
            LOGGER.warning("Could not create anonymous ytmusic client: %s", fallback_exc)
        return self._authenticated_client_or_none()

    def _anonymous_client_or_none(self) -> Any | None:
        try:
            self._client = self._create_client(authenticated=False)
            self._client_authenticated = False
            self._album_cache.clear()
            self._credits_cache.clear()
            self._lyrics_cache.clear()
            return self._client
        except Exception as exc:
            LOGGER.warning("Could not create anonymous ytmusic client for retry: %s", exc)
            return None

    def _authenticated_client_or_none(self) -> Any | None:
        if not (self.config.ytmusic_oauth_file.is_file() and self.auth_manager.client_credentials_available):
            return None
        try:
            self._client = self._create_client(authenticated=True)
            self._client_authenticated = True
            self.auth_manager.clear_refresh_failed()
            self._album_cache.clear()
            self._credits_cache.clear()
            self._lyrics_cache.clear()
            return self._client
        except Exception as exc:
            self.auth_manager.mark_refresh_failed()
            LOGGER.warning("Could not create authenticated ytmusic client: %s", exc)
            return None

    def _create_client(self, *, authenticated: bool) -> Any:
        factory = self._client_factory
        if factory is None:
            from ytmusicapi import YTMusic

            factory = YTMusic
        session = _timeout_session(self.config)
        kwargs = {
            "requests_session": session,
            "language": self.config.ytmusic_language,
            "location": self.config.ytmusic_location,
        }
        if authenticated and self.config.ytmusic_oauth_file.is_file() and self.auth_manager.client_credentials_available:
            oauth_factory = self._oauth_credentials_factory
            if oauth_factory is None:
                from ytmusicapi import OAuthCredentials

                oauth_factory = OAuthCredentials
            kwargs["oauth_credentials"] = oauth_factory(
                client_id=self.config.ytmusic_oauth_client_id,
                client_secret=self.config.ytmusic_oauth_client_secret,
                session=session,
            )
            return factory(str(self.config.ytmusic_oauth_file), **kwargs)
        return factory(**kwargs)


def _metadata_from_watch(track: dict[str, Any], watch: dict[str, Any], video_id: str) -> dict[str, Any]:
    artists = _artist_names(track.get("artists"))
    album = _album_name(track)
    album_id = _album_id(track)
    counterpart = track.get("counterpart") if isinstance(track.get("counterpart"), dict) else {}
    metadata: dict[str, Any] = {
        "title": _text(track.get("title")),
        "track": _text(track.get("title")),
        "artist": ", ".join(artists) if artists else None,
        "artists": artists or None,
        "album": album,
        "year": _text(track.get("year")),
        "ytmusic_video_id": _text(track.get("videoId")) or video_id,
        "ytmusic_album_id": album_id,
        "ytmusic_playlist_id": _text(watch.get("playlistId")),
        "ytmusic_lyrics_id": _text(watch.get("lyrics")),
        "ytmusic_related_id": _text(watch.get("related")),
        "ytmusic_counterpart_video_id": _text(counterpart.get("videoId")) if counterpart else None,
        "ytmusic_video_type": _text(track.get("videoType")),
        "ytmusic_is_explicit": track.get("isExplicit"),
        "ytmusic_watch_thumbnails": _thumbnails(track),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _metadata_from_album(album: dict[str, Any], video_id: str) -> dict[str, Any]:
    album_artists = _artist_names(album.get("artists"))
    album_track = _find_album_track(album, video_id)
    metadata: dict[str, Any] = {
        "album": _text(album.get("title")),
        "albumartist": ", ".join(album_artists) if album_artists else None,
        "albumartists": album_artists or None,
        "year": _text(album.get("year")),
        "track_total": _optional_int(album.get("trackCount")),
        "ytmusic_album_thumbnails": _thumbnails(album),
    }
    if album_track:
        metadata.update(
            {
                "track_number": _optional_int(album_track.get("trackNumber")),
                "ytmusic_credits_browse_id": _text(album_track.get("creditsBrowseId")),
                "ytmusic_is_explicit": album_track.get("isExplicit"),
            }
        )
    return {key: value for key, value in metadata.items() if value is not None}


def _metadata_from_credits(credits: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_credits(credits)
    writers = _section_data(sanitized.get("written_by"))
    composers = [str(writer) for writer in writers if writer]
    metadata: dict[str, Any] = {"ytmusic_credits": sanitized}
    if composers:
        metadata["composer"] = ", ".join(composers)
        metadata["composers"] = composers
    return metadata


def _metadata_from_lyrics(lyrics: dict[str, Any]) -> dict[str, Any]:
    text = _lyrics_text(lyrics.get("lyrics"))
    source = _text(lyrics.get("source"))
    metadata: dict[str, Any] = {}
    if text:
        metadata["lyrics"] = text
    if source:
        metadata["lyrics_source"] = source
    return metadata


def _select_watch_track(watch: dict[str, Any], video_id: str) -> dict[str, Any] | None:
    tracks = watch.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return None
    for track in tracks:
        if isinstance(track, dict) and track.get("videoId") == video_id:
            return track
    for track in tracks:
        counterpart = track.get("counterpart") if isinstance(track, dict) else None
        if isinstance(counterpart, dict) and counterpart.get("videoId") == video_id:
            return track
    return tracks[0] if isinstance(tracks[0], dict) else None


def _needs_song_search_fallback(metadata: dict[str, Any]) -> bool:
    video_type = _text(metadata.get("ytmusic_video_type"))
    if video_type not in VIDEO_TYPES_WITH_SPARSE_WATCH_METADATA:
        return False
    return not (_text(metadata.get("ytmusic_album_id")) or _text(metadata.get("album")))


def _song_search_query(item: PreflightItem, track: dict[str, Any]) -> str | None:
    title = _query_title(_text(track.get("title")) or item.title or _text(item.metadata.get("title")))
    artists = _artist_names(track.get("artists")) or _artist_values(
        item.metadata.get("artists"),
        item.metadata.get("artist"),
        item.artist,
        item.metadata.get("creator"),
        item.metadata.get("channel"),
    )
    if title and artists:
        return f"{artists[0]} {title}"
    if title:
        return title
    return None


def _query_title(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _strip_match_title_noise(value)
    parsed = parse_artist_title(cleaned)
    if parsed:
        cleaned = parsed[1]
    return _text(cleaned)


def _select_search_song(
    results: list[dict[str, Any]],
    *,
    item: PreflightItem,
    track: dict[str, Any],
) -> dict[str, Any] | None:
    target_titles = _title_match_values(_text(track.get("title")) or item.title or _text(item.metadata.get("title")))
    if not target_titles:
        return None
    target_artists = _normalized_artist_values(
        track.get("artists"),
        item.metadata.get("artists"),
        item.metadata.get("artist"),
        item.artist,
        item.metadata.get("creator"),
        item.metadata.get("channel"),
    )
    target_duration = _duration_seconds(item.metadata.get("duration")) or _duration_seconds(track.get("duration"))

    for result in results:
        if _text(result.get("resultType")) != "song":
            continue
        candidate_titles = _title_match_values(_text(result.get("title")))
        if not target_titles.intersection(candidate_titles):
            continue
        candidate_artists = _normalized_artist_values(result.get("artists"), result.get("artist"))
        if target_artists and candidate_artists and not target_artists.intersection(candidate_artists):
            continue
        candidate_duration = _duration_seconds(result.get("duration"))
        if target_duration and candidate_duration and not _duration_close(target_duration, candidate_duration):
            continue
        return result
    return None


def _merge_missing_search_result_fields(track: dict[str, Any], search_result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(track)
    for key in ("album", "artists", "duration", "title", "videoId", "videoType", "year", "isExplicit"):
        if not _has_loggable_value(merged.get(key)) and _has_loggable_value(search_result.get(key)):
            merged[key] = search_result[key]
    if not _thumbnails(merged) and _thumbnails(search_result):
        merged["thumbnail"] = search_result.get("thumbnail") or search_result.get("thumbnails")
    return merged


def _title_match_values(value: str | None) -> set[str]:
    if not value:
        return set()
    candidates = {value, clean_track_title(value), _strip_match_title_noise(value)}
    parsed = parse_artist_title(_strip_match_title_noise(value))
    if parsed:
        candidates.add(parsed[1])
    return {_match_text(candidate) for candidate in candidates if _match_text(candidate)}


def _strip_match_title_noise(value: str) -> str:
    cleaned = clean_track_title(value)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = TITLE_SUFFIX_NOISE_RE.sub("", cleaned).strip()
    return cleaned


def _normalized_artist_values(*values: Any) -> set[str]:
    return {_match_text(artist) for artist in _artist_values(*values) if _match_text(artist)}


def _artist_values(*values: Any) -> list[str]:
    artists: list[str] = []
    for value in values:
        if isinstance(value, list):
            artists.extend(_artist_names(value))
            continue
        text = _text(value)
        if not text:
            continue
        artists.extend(part.strip() for part in text.split(",") if part.strip())
    return artists


def _match_text(value: str) -> str:
    return MATCH_TEXT_RE.sub(" ", value.casefold()).strip()


def _duration_seconds(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    text = _text(value)
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total or None


def _duration_close(first: int, second: int) -> bool:
    tolerance = max(3, round(first * 0.05))
    return abs(first - second) <= tolerance


def _find_album_track(album: dict[str, Any], video_id: str) -> dict[str, Any] | None:
    tracks = album.get("tracks")
    if not isinstance(tracks, list):
        return None
    for track in tracks:
        if isinstance(track, dict) and track.get("videoId") == video_id:
            return track
    return None


def _album_name(track: dict[str, Any]) -> str | None:
    album = track.get("album")
    if isinstance(album, dict):
        return _text(album.get("name"))
    return _text(album)


def _album_id(track: dict[str, Any]) -> str | None:
    album = track.get("album")
    if isinstance(album, dict):
        return _text(album.get("id"))
    return None


def _artist_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for artist in value:
        if isinstance(artist, dict):
            name = _text(artist.get("name"))
        else:
            name = _text(artist)
        if name:
            names.append(name)
    return names


def _thumbnails(value: dict[str, Any]) -> list[dict[str, Any]]:
    raw = value.get("thumbnails") or value.get("thumbnail") or []
    if not isinstance(raw, list):
        return []
    thumbnails: list[dict[str, Any]] = []
    for thumbnail in raw:
        if not isinstance(thumbnail, dict):
            continue
        url = _text(thumbnail.get("url"))
        if not url:
            continue
        thumbnails.append(
            {
                "url": url,
                "width": _optional_int(thumbnail.get("width")) or 0,
                "height": _optional_int(thumbnail.get("height")) or 0,
            }
        )
    return thumbnails


def _select_artwork(metadata: dict[str, Any]) -> dict[str, Any] | None:
    album_thumbnails = metadata.pop("ytmusic_album_thumbnails", []) or []
    watch_thumbnails = metadata.pop("ytmusic_watch_thumbnails", []) or []
    selected = _largest_squareish_thumbnail(album_thumbnails) or _largest_squareish_thumbnail(watch_thumbnails)
    if not selected:
        return None
    return {
        "ytmusic_artwork_url": selected["url"],
        "ytmusic_artwork_width": selected.get("width") or 0,
        "ytmusic_artwork_height": selected.get("height") or 0,
    }


def _drop_internal_metadata(metadata: dict[str, Any]) -> None:
    for key in ("ytmusic_album_thumbnails", "ytmusic_watch_thumbnails", "ytmusic_credits_browse_id"):
        metadata.pop(key, None)


def _largest_squareish_thumbnail(thumbnails: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        thumbnail
        for thumbnail in thumbnails
        if thumbnail.get("url") and _is_squareish(thumbnail.get("width") or 0, thumbnail.get("height") or 0)
    ]
    if not candidates:
        candidates = [thumbnail for thumbnail in thumbnails if thumbnail.get("url")]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.get("width") or 0) * (item.get("height") or 0))


def _is_squareish(width: int, height: int) -> bool:
    if width <= 0 or height <= 0:
        return True
    ratio = width / height
    return 0.85 <= ratio <= 1.15


def _sanitize_credits(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, section in value.items():
        if key in SENSITIVE_KEYS:
            continue
        if key == "other_sections" and isinstance(section, list):
            sanitized[key] = [_sanitize_credit_section(item) for item in section if isinstance(item, dict)]
        elif isinstance(section, dict):
            sanitized[key] = _sanitize_credit_section(section)
    return sanitized


def _sanitize_credit_section(section: dict[str, Any]) -> dict[str, Any]:
    data = section.get("data")
    clean_data = [str(item) for item in data if item] if isinstance(data, list) else []
    return {
        "localized_title": _text(section.get("localized_title")),
        "data": clean_data,
    }


def _section_data(section: Any) -> list[str]:
    if not isinstance(section, dict):
        return []
    data = section.get("data")
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item]


def _lyrics_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        lines: list[str] = []
        for line in value:
            if isinstance(line, str):
                text = line
            else:
                text = getattr(line, "text", "")
            if text:
                lines.append(str(text))
        return "\n".join(lines).strip() or None
    return None


def _mapping_from_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    result: dict[str, Any] = {}
    for key in ("lyrics", "source", "hasTimestamps"):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    fields: list[str] = []
    for field in LOGGED_METADATA_FIELDS:
        after_value = after.get(field)
        if not _has_loggable_value(after_value):
            continue
        if before.get(field) != after_value:
            fields.append(field)
    return fields


def _metadata_added_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    fields: list[str] = []
    for field in LOGGED_METADATA_FIELDS:
        if _has_loggable_value(after.get(field)) and not _has_loggable_value(before.get(field)):
            fields.append(field)
    return fields


def _missing_rich_fields(metadata: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if not _has_loggable_value(metadata.get("album")):
        missing.append("album")
    if not _has_loggable_value(metadata.get("lyrics")):
        missing.append("lyrics")
    if not _has_loggable_value(metadata.get("ytmusic_credits")):
        missing.append("credits")
    if not _has_loggable_value(metadata.get("track_number")):
        missing.append("track_number")
    if not _has_loggable_value(metadata.get("track_total")):
        missing.append("track_total")
    return missing


def _has_loggable_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _field_list(fields: set[str] | list[str]) -> str:
    return ",".join(sorted(fields)) if fields else "none"


def _client_label(authenticated: bool) -> str:
    return "authenticated" if authenticated else "anonymous"


def _log_value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, list):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if not text:
        return "unknown"
    if len(text) > MAX_LOG_VALUE_LENGTH:
        return f"{text[: MAX_LOG_VALUE_LENGTH - 3]}..."
    return text


def _timeout_session(config: AppConfig) -> requests.Session:
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=config.ytmusic_request_timeout)  # type: ignore[method-assign]
    return session
