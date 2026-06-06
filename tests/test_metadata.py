import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.metadata import (
    normalize_track_metadata,
    parse_artist_title,
    tag_values_from_metadata,
)


class MetadataTests(unittest.TestCase):
    def test_parses_artist_and_track_from_youtube_title(self) -> None:
        metadata = normalize_track_metadata(
            {
                "title": "sma$her & MXZI - ACELERADA | Car Music",
                "channel": "Niza",
                "release_date": "20251119",
            }
        )

        self.assertEqual(metadata["artist"], "sma$her & MXZI")
        self.assertEqual(metadata["albumartist"], "sma$her & MXZI")
        self.assertEqual(metadata["track"], "ACELERADA")

    def test_channel_is_fallback_artist_when_title_cannot_be_split(self) -> None:
        metadata = normalize_track_metadata({"title": "Untitled jam", "channel": "Uploader"})

        self.assertEqual(metadata["artist"], "Uploader")
        self.assertEqual(metadata["track"], "Untitled jam")

    def test_uploader_does_not_override_parsed_artist(self) -> None:
        metadata = normalize_track_metadata({"title": "Real Artist - Real Track", "creator": "Uploader"})

        self.assertEqual(metadata["artist"], "Real Artist")
        self.assertEqual(metadata["track"], "Real Track")

    def test_tag_values_include_baseline_file_tags(self) -> None:
        tags = tag_values_from_metadata(
            {
                "title": "sma$her & MXZI - ACELERADA | Car Music",
                "release_date": "20251119",
                "playlist_index": "3",
            }
        )

        self.assertEqual(
            tags,
            {
                "title": "ACELERADA",
                "artist": "sma$her & MXZI",
                "albumartist": "sma$her & MXZI",
                "year": 2025,
                "track": 3,
            },
        )

    def test_parses_common_dash_variants(self) -> None:
        self.assertEqual(parse_artist_title("Artist – Track"), ("Artist", "Track"))
        self.assertEqual(parse_artist_title("Artist — Track"), ("Artist", "Track"))

    def test_tag_values_include_enriched_ytmusic_fields(self) -> None:
        tags = tag_values_from_metadata(
            {
                "track": "Song",
                "artist": "Artist",
                "artists": ["Artist", "Guest"],
                "album": "Album",
                "albumartist": "Album Artist",
                "albumartists": ["Album Artist"],
                "year": "2026",
                "track_number": "2",
                "track_total": "10",
                "lyrics": "Line 1\nLine 2",
                "composer": "Writer",
                "composers": ["Writer", "Co Writer"],
                "ytmusic_artwork_url": "https://example.com/cover.jpg",
            }
        )

        self.assertEqual(tags["artists"], ["Artist", "Guest"])
        self.assertEqual(tags["albumartists"], ["Album Artist"])
        self.assertEqual(tags["year"], 2026)
        self.assertEqual(tags["tracktotal"], 10)
        self.assertEqual(tags["lyrics"], "Line 1\nLine 2")
        self.assertEqual(tags["composer"], "Writer")
        self.assertEqual(tags["composers"], ["Writer", "Co Writer"])
        self.assertEqual(tags["artwork_url"], "https://example.com/cover.jpg")


if __name__ == "__main__":
    unittest.main()
