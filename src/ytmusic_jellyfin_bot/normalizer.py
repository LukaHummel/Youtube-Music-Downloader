from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from .models import NormalizedRequest, RequestKind


SUPPORTED_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}


class NormalizationError(ValueError):
    pass


def normalize_url(raw_url: str, forced_kind: RequestKind | None = None) -> NormalizedRequest:
    url = raw_url.strip()
    if not url:
        raise NormalizationError("No URL was provided.")
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in SUPPORTED_HOSTS:
        raise NormalizationError("Only YouTube and YouTube Music URLs are supported.")

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/")
        if not video_id:
            raise NormalizationError("The short YouTube URL is missing a video ID.")
        return NormalizedRequest(
            source_url=url,
            normalized_url=f"https://music.youtube.com/watch?v={video_id}",
            request_kind=RequestKind.TRACK,
            youtube_video_id=video_id,
        )

    query = parse_qs(parsed.query)
    if parsed.path == "/playlist" or forced_kind is RequestKind.PLAYLIST:
        playlist_id = query.get("list", [None])[0]
        if not playlist_id:
            raise NormalizationError("The playlist URL is missing a list ID.")
        return NormalizedRequest(
            source_url=url,
            normalized_url=f"https://music.youtube.com/playlist?list={playlist_id}",
            request_kind=RequestKind.PLAYLIST,
            playlist_id=playlist_id,
        )

    if parsed.path == "/watch":
        video_id = query.get("v", [None])[0]
        if not video_id:
            raise NormalizationError("The watch URL is missing a video ID.")
        if forced_kind is RequestKind.PLAYLIST:
            playlist_id = query.get("list", [None])[0]
            if not playlist_id:
                raise NormalizationError("Forced playlist handling requires a list ID.")
            return NormalizedRequest(
                source_url=url,
                normalized_url=f"https://music.youtube.com/playlist?list={playlist_id}",
                request_kind=RequestKind.PLAYLIST,
                youtube_video_id=video_id,
                playlist_id=playlist_id,
            )
        return NormalizedRequest(
            source_url=url,
            normalized_url=f"https://music.youtube.com/watch?v={video_id}",
            request_kind=RequestKind.TRACK,
            youtube_video_id=video_id,
            playlist_id=query.get("list", [None])[0],
        )

    raise NormalizationError("Unsupported YouTube URL format.")
