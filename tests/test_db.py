import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.db import Database
from ytmusic_jellyfin_bot.models import JobStatus, PreflightItem, RequestKind


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
        self.assertIsNone(loaded.progress_message_id)

    def test_progress_message_id_round_trips(self) -> None:
        job = self.db.create_job(
            source_url="https://youtube.com/watch?v=abc",
            normalized_url="https://music.youtube.com/watch?v=abc",
            request_kind=RequestKind.TRACK,
            chat_id=1,
            user_id=2,
            requested_by="tester",
            source_id="abc",
        )

        self.db.update_job(job.id, progress_message_id=1234)

        loaded = self.db.get_job(job.id)
        self.assertEqual(loaded.progress_message_id, 1234)

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

    def test_migration_adds_progress_message_id_to_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "old.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_url TEXT NOT NULL,
                        normalized_url TEXT NOT NULL,
                        request_kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        requested_by TEXT,
                        source_id TEXT,
                        source_title TEXT,
                        playlist_title TEXT,
                        current_item_index INTEGER NOT NULL DEFAULT 0,
                        total_items INTEGER NOT NULL DEFAULT 0,
                        progress_percent REAL,
                        progress_eta_seconds INTEGER,
                        progress_speed TEXT,
                        error_message TEXT,
                        result_summary TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        started_at TEXT,
                        finished_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO jobs (
                        source_url, normalized_url, request_kind, status, chat_id, user_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "https://youtube.com/watch?v=abc",
                        "https://music.youtube.com/watch?v=abc",
                        RequestKind.TRACK,
                        JobStatus.DOWNLOADING,
                        1,
                        2,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            migrated = Database(db_path)

            connection = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
            finally:
                connection.close()
            self.assertIn("progress_message_id", columns)
            loaded = migrated.get_job(1)
            self.assertEqual(loaded.status, JobStatus.QUEUED)
            self.assertIsNone(loaded.progress_message_id)

    def test_startup_requeues_non_terminal_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "app.db"
            db = Database(db_path)
            job_ids = []
            active_statuses = (
                JobStatus.NORMALIZING,
                JobStatus.PREFLIGHT,
                JobStatus.DOWNLOADING,
                JobStatus.RETAGGING,
                JobStatus.PLAYLIST_BUILDING,
            )
            for status in active_statuses:
                job = db.create_job(
                    source_url=f"https://youtube.com/watch?v={status}",
                    normalized_url=f"https://music.youtube.com/watch?v={status}",
                    request_kind=RequestKind.TRACK,
                    chat_id=1,
                    user_id=2,
                    requested_by="tester",
                    source_id=str(status),
                )
                db.update_job(job.id, status=status, progress_message_id=1000 + job.id)
                job_ids.append(job.id)

            restarted = Database(db_path)

            for job_id in job_ids:
                loaded = restarted.get_job(job_id)
                self.assertEqual(loaded.status, JobStatus.QUEUED)
                self.assertEqual(loaded.progress_message_id, 1000 + job_id)


if __name__ == "__main__":
    unittest.main()
