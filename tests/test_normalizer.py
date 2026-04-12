import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.models import RequestKind
from ytmusic_jellyfin_bot.normalizer import NormalizationError, normalize_url


class NormalizerTests(unittest.TestCase):
    def test_watch_url_defaults_to_track(self) -> None:
        result = normalize_url("https://www.youtube.com/watch?v=abc123&list=ignored")
        self.assertEqual(result.request_kind, RequestKind.TRACK)
        self.assertEqual(result.normalized_url, "https://music.youtube.com/watch?v=abc123")

    def test_forced_playlist_uses_list_id(self) -> None:
        result = normalize_url(
            "https://www.youtube.com/watch?v=abc123&list=PL123",
            forced_kind=RequestKind.PLAYLIST,
        )
        self.assertEqual(result.request_kind, RequestKind.PLAYLIST)
        self.assertEqual(result.normalized_url, "https://music.youtube.com/playlist?list=PL123")

    def test_short_url_is_normalized(self) -> None:
        result = normalize_url("https://youtu.be/abc123")
        self.assertEqual(result.normalized_url, "https://music.youtube.com/watch?v=abc123")

    def test_invalid_host_is_rejected(self) -> None:
        with self.assertRaises(NormalizationError):
            normalize_url("https://example.com/watch?v=abc123")


if __name__ == "__main__":
    unittest.main()
