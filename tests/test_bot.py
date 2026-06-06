import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.bot import _extract_supported_url


class BotTests(unittest.TestCase):
    def test_extracts_bare_music_url(self) -> None:
        self.assertEqual(
            _extract_supported_url("https://music.youtube.com/watch?v=abc123"),
            "https://music.youtube.com/watch?v=abc123",
        )

    def test_extracts_shared_url_from_surrounding_text(self) -> None:
        self.assertEqual(
            _extract_supported_url("Track title\nhttps://youtu.be/abc123?si=share-token."),
            "https://youtu.be/abc123?si=share-token",
        )

    def test_extracts_scheme_less_url(self) -> None:
        self.assertEqual(
            _extract_supported_url("music.youtube.com/watch?v=abc123"),
            "https://music.youtube.com/watch?v=abc123",
        )

    def test_ignores_non_youtube_text(self) -> None:
        self.assertIsNone(_extract_supported_url("https://example.com/watch?v=abc123"))


if __name__ == "__main__":
    unittest.main()
