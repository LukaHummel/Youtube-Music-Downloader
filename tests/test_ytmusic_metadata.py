import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.models import PreflightItem, PreflightResult
from ytmusic_jellyfin_bot.ytmusic_auth import YtMusicAuthManager, YtMusicAuthStatus
from ytmusic_jellyfin_bot.ytmusic_metadata import YtMusicMetadataProvider


class FakeYtMusicClient:
    def __init__(self, *, watch=None, watches=None, search_results=None, albums=None, credits=None, lyrics=None):
        self.watch = watch or {}
        self.watches = watches or {}
        self.search_results = search_results or []
        self.search_calls = []
        self.albums = albums or {}
        self.credits = credits or {}
        self.lyrics = lyrics or {}

    def get_watch_playlist(self, *, videoId: str, limit: int) -> dict:
        if videoId in self.watches:
            return self.watches[videoId]
        return self.watch

    def search(self, query: str, *, filter: str, limit: int) -> list[dict]:
        self.search_calls.append((query, filter, limit))
        return self.search_results

    def get_album(self, album_id: str) -> dict:
        return self.albums[album_id]

    def get_song_credits(self, browse_id: str) -> dict:
        return self.credits[browse_id]

    def get_lyrics(self, browse_id: str, timestamps: bool = False) -> dict:
        return self.lyrics[browse_id]


def _config(path: Path, **overrides):
    values = {
        "ytmusic_metadata_enabled": True,
        "ytmusic_oauth_client_id": "client-id",
        "ytmusic_oauth_client_secret": "client-secret",
        "ytmusic_oauth_file": path,
        "ytmusic_language": "en",
        "ytmusic_location": "",
        "ytmusic_request_timeout": 10.0,
        "ytmusic_fetch_lyrics": True,
        "ytmusic_fetch_credits": True,
        "ytmusic_embed_artwork": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _item(video_id: str = "target") -> PreflightItem:
    return PreflightItem(
        item_index=1,
        source_url=f"https://music.youtube.com/watch?v={video_id}",
        normalized_url=f"https://music.youtube.com/watch?v={video_id}",
        youtube_video_id=video_id,
        playlist_item_id=None,
        title="Fallback",
        artist="Uploader",
        album=None,
        metadata={"title": "Fallback", "channel": "Uploader"},
    )


class YtMusicMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.oauth_file = Path(self.tempdir.name) / "oauth.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _provider(self, client, **overrides):
        config = _config(self.oauth_file, **overrides)
        auth = YtMusicAuthManager(config)
        return YtMusicMetadataProvider(config, auth, client_factory=lambda *args, **kwargs: client), auth

    async def test_uses_unauthenticated_client_without_token_file(self) -> None:
        calls = []
        client = FakeYtMusicClient()
        config = _config(self.oauth_file)
        auth = YtMusicAuthManager(config)
        provider = YtMusicMetadataProvider(
            config,
            auth,
            client_factory=lambda *args, **kwargs: calls.append((args, kwargs)) or client,
        )

        self.assertIs(provider._client_or_none(), client)
        self.assertEqual(calls[0][0], ())

    async def test_uses_unauthenticated_client_first_with_token_file(self) -> None:
        self.oauth_file.write_text("{}", encoding="utf-8")
        calls = []
        client = FakeYtMusicClient()
        config = _config(self.oauth_file)
        auth = YtMusicAuthManager(config)
        provider = YtMusicMetadataProvider(
            config,
            auth,
            client_factory=lambda *args, **kwargs: calls.append((args, kwargs)) or client,
            oauth_credentials_factory=lambda **kwargs: SimpleNamespace(),
        )

        self.assertIs(provider._client_or_none(), client)
        self.assertEqual(calls[0][0], ())

    async def test_anonymous_client_creation_failure_falls_back_to_authenticated_client(self) -> None:
        self.oauth_file.write_text("{}", encoding="utf-8")
        client = FakeYtMusicClient()
        config = _config(self.oauth_file)
        auth = YtMusicAuthManager(config)

        def client_factory(*args, **kwargs):
            if not args:
                raise RuntimeError("anonymous failed")
            return client

        provider = YtMusicMetadataProvider(
            config,
            auth,
            client_factory=client_factory,
            oauth_credentials_factory=lambda **kwargs: SimpleNamespace(),
        )

        self.assertIs(provider._client_or_none(), client)
        self.assertEqual(auth.status(), YtMusicAuthStatus.AUTHENTICATED)

    async def test_authenticated_client_creation_failure_marks_refresh_failed_after_anonymous_failure(self) -> None:
        self.oauth_file.write_text("{}", encoding="utf-8")
        config = _config(self.oauth_file)
        auth = YtMusicAuthManager(config)

        def client_factory(*args, **kwargs):
            raise RuntimeError("client failed")

        provider = YtMusicMetadataProvider(
            config,
            auth,
            client_factory=client_factory,
            oauth_credentials_factory=lambda **kwargs: SimpleNamespace(),
        )

        self.assertIsNone(provider._client_or_none())
        self.assertEqual(auth.status(), YtMusicAuthStatus.AUTHENTICATED_REFRESH_FAILED)

    async def test_authenticated_lookup_failure_retries_anonymously(self) -> None:
        self.oauth_file.write_text("{}", encoding="utf-8")
        authenticated = FakeYtMusicClient()

        def failing_watch(*, videoId: str, limit: int) -> dict:
            raise RuntimeError("Server returned HTTP 400: Bad Request.")

        authenticated.get_watch_playlist = failing_watch
        anonymous = FakeYtMusicClient(
            watch={
                "tracks": [
                    {
                        "videoId": "target",
                        "title": "Anonymous Song",
                        "artists": [{"name": "Artist"}],
                    }
                ]
            }
        )
        calls = []
        config = _config(self.oauth_file)
        auth = YtMusicAuthManager(config)

        def client_factory(*args, **kwargs):
            calls.append(args)
            return authenticated if args else anonymous

        provider = YtMusicMetadataProvider(
            config,
            auth,
            client_factory=client_factory,
            oauth_credentials_factory=lambda **kwargs: SimpleNamespace(),
        )
        provider._client = authenticated
        provider._client_authenticated = True

        result = await provider.enrich_item(_item())

        self.assertEqual(result.title, "Anonymous Song")
        self.assertEqual(calls, [()])
        self.assertFalse(provider._client_authenticated)

    async def test_anonymous_lookup_failure_retries_authenticated(self) -> None:
        self.oauth_file.write_text("{}", encoding="utf-8")
        anonymous = FakeYtMusicClient()

        def failing_watch(*, videoId: str, limit: int) -> dict:
            raise RuntimeError("anonymous failed")

        anonymous.get_watch_playlist = failing_watch
        authenticated = FakeYtMusicClient(
            watch={
                "tracks": [
                    {
                        "videoId": "target",
                        "title": "Authenticated Song",
                        "artists": [{"name": "Artist"}],
                    }
                ]
            }
        )
        calls = []
        config = _config(self.oauth_file)
        auth = YtMusicAuthManager(config)

        def client_factory(*args, **kwargs):
            calls.append(args)
            return authenticated if args else anonymous

        provider = YtMusicMetadataProvider(
            config,
            auth,
            client_factory=client_factory,
            oauth_credentials_factory=lambda **kwargs: SimpleNamespace(),
        )

        result = await provider.enrich_item(_item())

        self.assertEqual(result.title, "Authenticated Song")
        self.assertEqual(calls, [(), (str(self.oauth_file),)])
        self.assertTrue(provider._client_authenticated)

    async def test_watch_playlist_exact_video_match(self) -> None:
        client = FakeYtMusicClient(
            watch={
                "tracks": [
                    {"videoId": "other", "title": "Other"},
                    {
                        "videoId": "target",
                        "title": "Song",
                        "artists": [{"name": "Artist"}],
                        "album": {"name": "Album", "id": "ALB"},
                        "year": "2026",
                    },
                ]
            },
            albums={"ALB": {}},
        )
        provider, _auth = self._provider(client)

        result = await provider.enrich_item(_item())

        self.assertEqual(result.title, "Song")
        self.assertEqual(result.artist, "Artist")
        self.assertEqual(result.album, "Album")
        self.assertEqual(result.metadata["ytmusic_album_id"], "ALB")

    async def test_watch_playlist_counterpart_match(self) -> None:
        client = FakeYtMusicClient(
            watch={
                "tracks": [
                    {
                        "videoId": "music-id",
                        "title": "Counterpart Song",
                        "artists": [{"name": "Artist"}],
                        "counterpart": {"videoId": "target"},
                    }
                ]
            }
        )
        provider, _auth = self._provider(client)

        result = await provider.enrich_item(_item())

        self.assertEqual(result.title, "Counterpart Song")
        self.assertEqual(result.metadata["ytmusic_counterpart_video_id"], "target")

    async def test_sparse_video_uses_exact_song_search_fallback(self) -> None:
        client = FakeYtMusicClient(
            watches={
                "video": {
                    "playlistId": "VIDEO-PL",
                    "tracks": [
                        {
                            "videoId": "video",
                            "title": "Artist - Song - Official Visualizer",
                            "artists": [{"name": "Artist"}],
                            "videoType": "MUSIC_VIDEO_TYPE_OMV",
                            "duration": "3:30",
                        }
                    ],
                },
                "song-id": {
                    "playlistId": "SONG-PL",
                    "lyrics": "LYRICS",
                    "tracks": [
                        {
                            "videoId": "song-id",
                            "title": "Song",
                            "artists": [{"name": "Artist"}],
                            "album": {"name": "Album", "id": "ALB"},
                            "videoType": "MUSIC_VIDEO_TYPE_ATV",
                            "duration": "3:30",
                        }
                    ],
                },
            },
            search_results=[
                {
                    "resultType": "song",
                    "videoId": "song-id",
                    "title": "Song",
                    "artists": [{"name": "Artist"}],
                    "album": {"name": "Album", "id": "ALB"},
                    "duration": "3:30",
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                }
            ],
            albums={
                "ALB": {
                    "title": "Album",
                    "artists": [{"name": "Album Artist"}],
                    "year": "2026",
                    "trackCount": "9",
                    "tracks": [{"videoId": "song-id", "trackNumber": "4", "creditsBrowseId": "CREDITS"}],
                }
            },
            credits={"CREDITS": {"written_by": {"localized_title": "Written by", "data": ["Writer"]}}},
            lyrics={"LYRICS": {"lyrics": "Words", "source": "YouTube Music"}},
        )
        provider, _auth = self._provider(client)
        item = _item("video")
        item.metadata["duration"] = 210

        result = await provider.enrich_item(item)
        metadata = result.metadata

        self.assertEqual(client.search_calls, [("Artist Song", "songs", 10)])
        self.assertEqual(result.title, "Song")
        self.assertEqual(metadata["album"], "Album")
        self.assertEqual(metadata["albumartist"], "Album Artist")
        self.assertEqual(metadata["track_number"], 4)
        self.assertEqual(metadata["track_total"], 9)
        self.assertEqual(metadata["lyrics"], "Words")
        self.assertEqual(metadata["composer"], "Writer")
        self.assertEqual(metadata["ytmusic_video_id"], "song-id")
        self.assertEqual(metadata["ytmusic_counterpart_video_id"], "video")
        self.assertEqual(metadata["ytmusic_playlist_id"], "SONG-PL")

    async def test_sparse_video_does_not_accept_inexact_song_search_fallback(self) -> None:
        client = FakeYtMusicClient(
            watch={
                "playlistId": "VIDEO-PL",
                "tracks": [
                    {
                        "videoId": "video",
                        "title": "Rapture",
                        "artists": [{"name": "Evanescence"}],
                        "videoType": "MUSIC_VIDEO_TYPE_OMV",
                        "duration": "3:27",
                    }
                ],
            },
            search_results=[
                {
                    "resultType": "song",
                    "videoId": "wrong-song",
                    "title": "My Last Breath",
                    "artists": [{"name": "Evanescence"}],
                    "album": {"name": "Fallen", "id": "ALB"},
                    "duration": "3:27",
                }
            ],
        )
        provider, _auth = self._provider(client)

        with self.assertLogs("ytmusic_jellyfin_bot.ytmusic_metadata", level="INFO") as logs:
            result = await provider.enrich_item(_item("video"))

        self.assertEqual(client.search_calls, [("Evanescence Rapture", "songs", 10)])
        self.assertEqual(result.title, "Rapture")
        self.assertNotIn("album", result.metadata)
        self.assertEqual(result.metadata["ytmusic_video_id"], "video")
        output = "\n".join(logs.output)
        self.assertIn("ytmusic song search fallback not accepted", output)
        self.assertIn("missing_rich_fields=album,credits,lyrics,track_number,track_total", output)

    async def test_album_credits_lyrics_artwork_and_sanitization_merge(self) -> None:
        client = FakeYtMusicClient(
            watch={
                "playlistId": "PL",
                "lyrics": "LYRICS",
                "related": "RELATED",
                "tracks": [
                    {
                        "videoId": "target",
                        "title": "Song",
                        "artists": [{"name": "Artist"}],
                        "album": {"name": "Single", "id": "ALB"},
                        "thumbnail": [{"url": "https://example.com/small.jpg", "width": 60, "height": 60}],
                    }
                ],
            },
            albums={
                "ALB": {
                    "title": "Album",
                    "artists": [{"name": "Album Artist"}],
                    "year": "2025",
                    "trackCount": "12",
                    "thumbnails": [
                        {"url": "https://example.com/cover-60.jpg", "width": 60, "height": 60},
                        {"url": "https://example.com/cover-500.jpg", "width": 500, "height": 500},
                    ],
                    "tracks": [
                        {
                            "videoId": "target",
                            "trackNumber": "7",
                            "creditsBrowseId": "CREDITS",
                            "isExplicit": True,
                        }
                    ],
                }
            },
            credits={
                "CREDITS": {
                    "feedbackTokens": {"token": "secret"},
                    "written_by": {
                        "localized_title": "Written by",
                        "data": ["Writer", "Co Writer"],
                        "url": "https://secret.example",
                    },
                }
            },
            lyrics={"LYRICS": {"lyrics": "Line 1\nLine 2", "source": "YouTube Music"}},
        )
        provider, _auth = self._provider(client)

        result = await provider.enrich_item(_item())
        metadata = result.metadata

        self.assertEqual(metadata["album"], "Album")
        self.assertEqual(metadata["albumartist"], "Album Artist")
        self.assertEqual(metadata["track_number"], 7)
        self.assertEqual(metadata["track_total"], 12)
        self.assertEqual(metadata["lyrics"], "Line 1\nLine 2")
        self.assertEqual(metadata["lyrics_source"], "YouTube Music")
        self.assertEqual(metadata["composer"], "Writer, Co Writer")
        self.assertEqual(metadata["composers"], ["Writer", "Co Writer"])
        self.assertEqual(metadata["ytmusic_artwork_url"], "https://example.com/cover-500.jpg")
        self.assertEqual(metadata["ytmusic_playlist_id"], "PL")
        self.assertEqual(metadata["ytmusic_related_id"], "RELATED")
        self.assertTrue(metadata["ytmusic_is_explicit"])
        self.assertNotIn("feedbackTokens", metadata["ytmusic_credits"])
        self.assertNotIn("url", metadata["ytmusic_credits"]["written_by"])
        self.assertNotIn("ytmusic_album_thumbnails", metadata)
        self.assertNotIn("ytmusic_watch_thumbnails", metadata)

    async def test_enrichment_logs_added_value_without_sensitive_content(self) -> None:
        client = FakeYtMusicClient(
            watch={
                "playlistId": "PL",
                "lyrics": "LYRICS",
                "tracks": [
                    {
                        "videoId": "target",
                        "title": "Song",
                        "artists": [{"name": "Artist"}],
                        "album": {"name": "Single", "id": "ALB"},
                    }
                ],
            },
            albums={
                "ALB": {
                    "title": "Album",
                    "artists": [{"name": "Album Artist"}],
                    "year": "2025",
                    "trackCount": "12",
                    "thumbnails": [{"url": "https://example.com/cover.jpg", "width": 500, "height": 500}],
                    "tracks": [
                        {
                            "videoId": "target",
                            "trackNumber": "7",
                            "creditsBrowseId": "CREDITS",
                        }
                    ],
                }
            },
            credits={
                "CREDITS": {
                    "written_by": {
                        "localized_title": "Written by",
                        "data": ["Writer"],
                        "url": "https://secret.example",
                    },
                    "feedbackTokens": {"token": "secret-token"},
                }
            },
            lyrics={"LYRICS": {"lyrics": "Line 1\nLine 2", "source": "YouTube Music"}},
        )
        provider, _auth = self._provider(client)
        preflight = PreflightResult(
            source_id="target",
            source_title="Fallback",
            playlist_title=None,
            items=[_item()],
        )

        with self.assertLogs("ytmusic_jellyfin_bot.ytmusic_metadata", level="INFO") as logs:
            await provider.enrich_preflight(preflight)

        output = "\n".join(logs.output)
        self.assertIn("ytmusic metadata enrichment started", output)
        self.assertIn("ytmusic metadata result", output)
        self.assertIn("changed_fields=", output)
        self.assertIn("added_fields=", output)
        self.assertIn("lyrics=True", output)
        self.assertIn("credits=True", output)
        self.assertIn("artwork=True", output)
        self.assertIn("missing_rich_fields=none", output)
        self.assertIn("ytmusic metadata enrichment completed", output)
        self.assertNotIn("Line 1", output)
        self.assertNotIn("Line 2", output)
        self.assertNotIn("secret-token", output)
        self.assertNotIn("secret.example", output)


if __name__ == "__main__":
    unittest.main()
