from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import ItemStatus, JobItemRecord, JobRecord, JobStatus, PreflightItem, RequestKind


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS jobs (
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
                    progress_message_id INTEGER,
                    error_message TEXT,
                    result_summary TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS job_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    item_index INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    youtube_video_id TEXT,
                    playlist_item_id TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    status TEXT NOT NULL,
                    download_path TEXT,
                    final_path TEXT,
                    error_message TEXT,
                    mb_trackid TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    direction TEXT NOT NULL,
                    telegram_chat_id INTEGER,
                    telegram_user_id INTEGER,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS source_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL UNIQUE,
                    youtube_video_id TEXT,
                    playlist_item_id TEXT,
                    final_path TEXT NOT NULL,
                    mb_trackid TEXT,
                    artist TEXT,
                    album TEXT,
                    title TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self._migrate_jobs_table(connection)
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = CURRENT_TIMESTAMP, error_message = NULL
                WHERE status IN (?, ?, ?, ?, ?)
                """,
                (
                    JobStatus.QUEUED,
                    JobStatus.NORMALIZING,
                    JobStatus.PREFLIGHT,
                    JobStatus.DOWNLOADING,
                    JobStatus.RETAGGING,
                    JobStatus.PLAYLIST_BUILDING,
                ),
            )

    @staticmethod
    def _migrate_jobs_table(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
        if "progress_message_id" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN progress_message_id INTEGER")

    def create_job(
        self,
        *,
        source_url: str,
        normalized_url: str,
        request_kind: RequestKind,
        chat_id: int,
        user_id: int,
        requested_by: str | None,
        source_id: str | None,
    ) -> JobRecord:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    source_url, normalized_url, request_kind, status, chat_id, user_id,
                    requested_by, source_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_url,
                    normalized_url,
                    request_kind.value,
                    JobStatus.QUEUED,
                    chat_id,
                    user_id,
                    requested_by,
                    source_id,
                ),
            )
            job_id = cursor.lastrowid
        return self.get_job(job_id)

    def replace_job_items(self, job_id: int, items: list[PreflightItem]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM job_items WHERE job_id = ?", (job_id,))
            for item in items:
                connection.execute(
                    """
                    INSERT INTO job_items (
                        job_id, item_index, source_url, normalized_url, youtube_video_id,
                        playlist_item_id, title, artist, album, status, mb_trackid, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        item.item_index,
                        item.source_url,
                        item.normalized_url,
                        item.youtube_video_id,
                        item.playlist_item_id,
                        item.title,
                        item.artist,
                        item.album,
                        ItemStatus.PENDING,
                        item.metadata.get("mb_trackid"),
                        json.dumps(item.metadata, ensure_ascii=True),
                    ),
                )
            connection.execute(
                """
                UPDATE jobs
                SET total_items = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (len(items), job_id),
            )

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values())
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*values, job_id),
            )

    def update_job_item(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values())
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE job_items SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*values, item_id),
            )

    def get_job(self, job_id: int) -> JobRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job {job_id} was not found")
        return self._job_from_row(row)

    def get_job_items(self, job_id: int) -> list[JobItemRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_items WHERE job_id = ? ORDER BY item_index",
                (job_id,),
            ).fetchall()
        return [self._job_item_from_row(row) for row in rows]

    def list_recent_jobs(self, limit: int = 10) -> list[JobRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def next_queued_job(self) -> JobRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id ASC LIMIT 1",
                (JobStatus.QUEUED,),
            ).fetchone()
        return self._job_from_row(row) if row else None

    def get_active_job(self) -> JobRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN (?, ?, ?, ?, ?)
                ORDER BY id ASC
                LIMIT 1
                """,
                (
                    JobStatus.NORMALIZING,
                    JobStatus.PREFLIGHT,
                    JobStatus.DOWNLOADING,
                    JobStatus.RETAGGING,
                    JobStatus.PLAYLIST_BUILDING,
                ),
            ).fetchone()
        return self._job_from_row(row) if row else None

    def count_queued_jobs(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status = ?",
                (JobStatus.QUEUED,),
            ).fetchone()
        return int(row["count"])

    def mark_cancel_requested(self, job_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )

    def is_cancel_requested(self, job_id: int) -> bool:
        return self.get_job(job_id).cancel_requested

    def cancel_if_queued(self, job_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ?
                """,
                (JobStatus.CANCELLED, job_id, JobStatus.QUEUED),
            )
            return cursor.rowcount > 0

    def requeue_job(self, job_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = NULL, result_summary = NULL,
                    cancel_requested = 0, started_at = NULL, finished_at = NULL,
                    progress_percent = NULL, progress_eta_seconds = NULL,
                    progress_speed = NULL, current_item_index = 0, total_items = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (JobStatus.QUEUED, job_id, JobStatus.FAILED, JobStatus.PARTIAL, JobStatus.CANCELLED),
            )
            if cursor.rowcount:
                connection.execute("DELETE FROM job_items WHERE job_id = ?", (job_id,))
                return True
        return False

    def add_message(
        self,
        *,
        direction: str,
        body: str,
        job_id: int | None = None,
        telegram_chat_id: int | None = None,
        telegram_user_id: int | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (job_id, direction, telegram_chat_id, telegram_user_id, body)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, direction, telegram_chat_id, telegram_user_id, body),
            )

    def upsert_source_mapping(
        self,
        *,
        source_key: str,
        youtube_video_id: str | None,
        playlist_item_id: str | None,
        final_path: str,
        mb_trackid: str | None,
        artist: str | None,
        album: str | None,
        title: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_mappings (
                    source_key, youtube_video_id, playlist_item_id, final_path,
                    mb_trackid, artist, album, title
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    youtube_video_id = excluded.youtube_video_id,
                    playlist_item_id = excluded.playlist_item_id,
                    final_path = excluded.final_path,
                    mb_trackid = excluded.mb_trackid,
                    artist = excluded.artist,
                    album = excluded.album,
                    title = excluded.title,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    source_key,
                    youtube_video_id,
                    playlist_item_id,
                    final_path,
                    mb_trackid,
                    artist,
                    album,
                    title,
                ),
            )

    def find_source_mapping(self, source_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_mappings WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def source_key_for_video(video_id: str) -> str:
        return f"youtube:video:{video_id}"

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            source_url=row["source_url"],
            normalized_url=row["normalized_url"],
            request_kind=RequestKind(row["request_kind"]),
            status=JobStatus(row["status"]),
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            requested_by=row["requested_by"],
            source_id=row["source_id"],
            source_title=row["source_title"],
            playlist_title=row["playlist_title"],
            current_item_index=row["current_item_index"],
            total_items=row["total_items"],
            progress_percent=row["progress_percent"],
            progress_eta_seconds=row["progress_eta_seconds"],
            progress_speed=row["progress_speed"],
            progress_message_id=row["progress_message_id"],
            error_message=row["error_message"],
            result_summary=row["result_summary"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _job_item_from_row(row: sqlite3.Row) -> JobItemRecord:
        return JobItemRecord(
            id=row["id"],
            job_id=row["job_id"],
            item_index=row["item_index"],
            source_url=row["source_url"],
            normalized_url=row["normalized_url"],
            youtube_video_id=row["youtube_video_id"],
            playlist_item_id=row["playlist_item_id"],
            title=row["title"],
            artist=row["artist"],
            album=row["album"],
            status=ItemStatus(row["status"]),
            download_path=row["download_path"],
            final_path=row["final_path"],
            error_message=row["error_message"],
            mb_trackid=row["mb_trackid"],
            metadata_json=row["metadata_json"],
        )
