import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.db import Database
from ytmusic_jellyfin_bot.models import PreflightItem, RequestKind


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "app.db")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_and_fetch_job(self) -> None:
        job = self.db.create_job(
            source_url="https://youtube.com/watch?v=abc",
            normalized_url="https://music.youtube.com/watch?v=abc",
            request_kind=RequestKind.TRACK,
            chat_id=1,
            user_id=2,
            requested_by="tester",
            source_id="abc",
        )
        loaded = self.db.get_job(job.id)
        self.assertEqual(loaded.source_id, "abc")
        self.assertEqual(loaded.request_kind, RequestKind.TRACK)

    def test_replace_job_items_updates_total(self) -> None:
        job = self.db.create_job(
            source_url="https://youtube.com/playlist?list=PL1",
            normalized_url="https://music.youtube.com/playlist?list=PL1",
            request_kind=RequestKind.PLAYLIST,
            chat_id=1,
            user_id=2,
            requested_by="tester",
            source_id="PL1",
        )
        self.db.replace_job_items(
            job.id,
            [
                PreflightItem(
                    item_index=1,
                    source_url="https://music.youtube.com/watch?v=abc",
                    normalized_url="https://music.youtube.com/watch?v=abc",
                    youtube_video_id="abc",
                    playlist_item_id="PL1:1",
                    title="Song",
                    artist="Artist",
                    album="Album",
                    metadata={"title": "Song"},
                )
            ],
        )
        refreshed = self.db.get_job(job.id)
        self.assertEqual(refreshed.total_items, 1)
        self.assertEqual(len(self.db.get_job_items(job.id)), 1)


if __name__ == "__main__":
    unittest.main()
