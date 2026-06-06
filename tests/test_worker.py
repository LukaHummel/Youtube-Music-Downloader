import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.db import Database
from ytmusic_jellyfin_bot.models import (
    DownloadResult,
    ImportResult,
    PreflightItem,
    PreflightResult,
    RequestKind,
)
from ytmusic_jellyfin_bot.worker import JobWorker


class FakeYtDlp:
    def __init__(self, audio_path: Path):
        self.audio_path = audio_path

    async def preflight(self, url: str, request_kind: RequestKind) -> PreflightResult:
        return PreflightResult(
            source_id="video",
            source_title="Raw Title",
            playlist_title=None,
            items=[
                PreflightItem(
                    item_index=1,
                    source_url="https://music.youtube.com/watch?v=video",
                    normalized_url="https://music.youtube.com/watch?v=video",
                    youtube_video_id="video",
                    playlist_item_id=None,
                    title="Raw Title",
                    artist="Raw Artist",
                    album=None,
                    metadata={"title": "Raw Title", "artist": "Raw Artist"},
                )
            ],
        )

    async def download_track(self, **kwargs) -> DownloadResult:
        return DownloadResult(audio_path=self.audio_path, info_json_path=None)


class FakeYtMusic:
    async def enrich_preflight(self, preflight: PreflightResult) -> PreflightResult:
        item = preflight.items[0]
        enriched = PreflightItem(
            item_index=item.item_index,
            source_url=item.source_url,
            normalized_url=item.normalized_url,
            youtube_video_id=item.youtube_video_id,
            playlist_item_id=item.playlist_item_id,
            title="Enriched Title",
            artist="Enriched Artist",
            album="Enriched Album",
            metadata={
                **item.metadata,
                "track": "Enriched Title",
                "artist": "Enriched Artist",
                "album": "Enriched Album",
            },
        )
        return PreflightResult(
            source_id=preflight.source_id,
            source_title=preflight.source_title,
            playlist_title=preflight.playlist_title,
            items=[enriched],
        )


class FakeBeets:
    def __init__(self):
        self.imported_metadata = None

    async def import_track(self, audio_path: Path, metadata: dict) -> ImportResult:
        self.imported_metadata = metadata
        return ImportResult(status="imported", final_path=audio_path)

    def write_baseline_tags(self, audio_path: Path, metadata: dict, *, overwrite: bool) -> None:
        pass


class FakePlaylistWriter:
    pass


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ytmusic_enrichment_is_stored_and_passed_to_beets(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            audio_path = temp_path / "track.m4a"
            audio_path.write_bytes(b"placeholder")
            db = Database(temp_path / "app.db")
            job = db.create_job(
                source_url="https://music.youtube.com/watch?v=video",
                normalized_url="https://music.youtube.com/watch?v=video",
                request_kind=RequestKind.TRACK,
                chat_id=1,
                user_id=2,
                requested_by="tester",
                source_id="video",
            )
            beets = FakeBeets()
            worker = JobWorker(
                config=SimpleNamespace(),
                db=db,
                ytdlp=FakeYtDlp(audio_path),
                beets=beets,
                playlist_writer=FakePlaylistWriter(),
                ytmusic=FakeYtMusic(),
            )

            await worker._process_job(job)

            item = db.get_job_items(job.id)[0]
            self.assertEqual(item.title, "Enriched Title")
            self.assertEqual(item.artist, "Enriched Artist")
            self.assertEqual(item.album, "Enriched Album")
            self.assertEqual(beets.imported_metadata["track"], "Enriched Title")
            self.assertEqual(beets.imported_metadata["artist"], "Enriched Artist")


if __name__ == "__main__":
    unittest.main()
