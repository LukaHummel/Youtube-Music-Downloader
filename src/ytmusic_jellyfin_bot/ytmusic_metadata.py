from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Mapping
from typing import Any, Callable

import requests

from .config import AppConfig
from .metadata import normalize_track_metadata
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

    def clear_client_cache(self) -> None:
        self._client = None
        self._client_authenticated = False
        self._album_cache.clear()
        self._credits_cache.clear()
        self._lyrics_cache.clear()

    async def enrich_preflight(self, preflight: PreflightResult) -> PreflightResult:
        if not self.config.ytmusic_metadata_enabled:
            return preflight
        items: list[PreflightItem] = []
        for item in preflight.items:
            items.append(await self.enrich_item(item))
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
            return item
        track = _select_watch_track(watch, item.youtube_video_id)
        if not track:
            return item
        enriched = dict(item.metadata)
        enriched.update(_metadata_from_watch(track, watch, item.youtube_video_id))

        album_id = _album_id(track)
        album = await self._get_album(client, album_id)
        if album:
            enriched.update(_metadata_from_album(album, item.youtube_video_id))

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
        LOGGER.info(
            "Enriched ytmusic metadata: video_id=%s title=%s artist=%s album=%s authenticated=%s",
            item.youtube_video_id,
            enriched.get("track") or enriched.get("title") or "unknown",
            enriched.get("artist") or "unknown",
            enriched.get("album") or "unknown",
            self._client_authenticated,
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


def _timeout_session(config: AppConfig) -> requests.Session:
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=config.ytmusic_request_timeout)  # type: ignore[method-assign]
    return session
