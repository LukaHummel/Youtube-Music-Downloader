from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .beets_runner import BeetsError, BeetsRunner
from .config import AppConfig
from .db import Database
from .models import ItemStatus, JobItemRecord, JobRecord, JobStatus, RequestKind
from .playlist_writer import PlaylistWriter
from .ytdlp_runner import YtDlpError, YtDlpRunner

Notifier = Callable[[int, str, int | None], Awaitable[None]]

LOGGER = logging.getLogger(__name__)


class JobWorker:
    def __init__(
        self,
        *,
        config: AppConfig,
        db: Database,
        ytdlp: YtDlpRunner,
        beets: BeetsRunner,
        playlist_writer: PlaylistWriter,
    ):
        self.config = config
        self.db = db
        self.ytdlp = ytdlp
        self.beets = beets
        self.playlist_writer = playlist_writer
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._stopping = False
        self._notifier: Notifier | None = None

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop(), name="job-worker")
            self.wake()

    async def stop(self) -> None:
        self._stopping = True
        self.wake()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def wake(self) -> None:
        self._wake_event.set()

    async def _run_loop(self) -> None:
        while not self._stopping:
            job = self.db.next_queued_job()
            if job is None:
                self._wake_event.clear()
                await self._wake_event.wait()
                continue
            try:
                await self._process_job(job)
            except Exception:
                LOGGER.exception("Unhandled job failure for job %s", job.id)
                self.db.update_job(
                    job.id,
                    status=JobStatus.FAILED,
                    error_message="Unhandled internal error.",
                    finished_at=self._timestamp(),
                )
                await self._notify(job.chat_id, f"Job #{job.id} failed with an internal error.", job.id)

    async def _process_job(self, job: JobRecord) -> None:
        self.db.update_job(
            job.id,
            status=JobStatus.NORMALIZING,
            started_at=self._timestamp(),
            finished_at=None,
            error_message=None,
            result_summary=None,
            current_item_index=0,
            progress_percent=None,
            progress_eta_seconds=None,
            progress_speed=None,
        )
        self.db.update_job(job.id, status=JobStatus.PREFLIGHT)

        try:
            preflight = await self.ytdlp.preflight(job.normalized_url, job.request_kind)
        except YtDlpError as exc:
            message = (
                "Private or restricted YouTube content requires a fresh mounted cookies.txt file."
                if exc.auth_required
                else str(exc)
            )
            self.db.update_job(
                job.id,
                status=JobStatus.FAILED,
                error_message=message,
                finished_at=self._timestamp(),
            )
            await self._notify(job.chat_id, f"Job #{job.id} failed during preflight.\n{message}", job.id)
            return

        self.db.replace_job_items(job.id, preflight.items)
        self.db.update_job(
            job.id,
            source_id=preflight.source_id,
            source_title=preflight.source_title,
            playlist_title=preflight.playlist_title,
            total_items=len(preflight.items),
        )

        track_paths: list[Path] = []
        imported_count = 0
        duplicate_count = 0
        failed_count = 0
        ambiguous_count = 0

        for item in self.db.get_job_items(job.id):
            if self.db.is_cancel_requested(job.id):
                self._cancel_remaining_items(job.id)
                self.db.update_job(
                    job.id,
                    status=JobStatus.CANCELLED,
                    result_summary=f"Cancelled after {imported_count} imported items.",
                    finished_at=self._timestamp(),
                )
                await self._notify(job.chat_id, f"Job #{job.id} was cancelled.", job.id)
                return

            source_key = self.db.source_key_for_video(item.youtube_video_id or "")
            mapping = self.db.find_source_mapping(source_key)
            if mapping and Path(mapping["final_path"]).exists():
                duplicate_count += 1
                final_path = Path(mapping["final_path"])
                self.db.update_job(
                    job.id,
                    status=JobStatus.DOWNLOADING,
                    current_item_index=item.item_index,
                )
                self.db.update_job_item(
                    item.id,
                    status=ItemStatus.SKIPPED_EXISTING,
                    final_path=str(final_path),
                )
                track_paths.append(final_path)
                continue

            self.db.update_job(
                job.id,
                status=JobStatus.DOWNLOADING,
                current_item_index=item.item_index,
            )
            self.db.update_job_item(item.id, status=ItemStatus.DOWNLOADING)

            try:
                download_result = await self.ytdlp.download_track(
                    job_id=job.id,
                    item_index=item.item_index,
                    url=item.normalized_url,
                    progress_callback=lambda percent, eta, speed: self._update_progress(
                        job.id, percent, eta, speed
                    ),
                )
            except YtDlpError as exc:
                failed_count += 1
                failure_message = (
                    "Private or restricted content could not be downloaded. Refresh the mounted cookies.txt file."
                    if exc.auth_required
                    else str(exc)
                )
                self.db.update_job_item(item.id, status=ItemStatus.FAILED, error_message=failure_message)
                continue

            self.db.update_job(job.id, status=JobStatus.RETAGGING)
            self.db.update_job_item(
                item.id,
                status=ItemStatus.RETAGGING,
                download_path=str(download_result.audio_path),
            )
            metadata = self._load_metadata(item, download_result.info_json_path)

            try:
                import_result = await self.beets.import_track(download_result.audio_path, metadata)
            except BeetsError as exc:
                failed_count += 1
                self.db.update_job_item(item.id, status=ItemStatus.FAILED, error_message=str(exc))
                continue

            title = metadata.get("track") or metadata.get("title")
            artist = metadata.get("artist") or metadata.get("albumartist") or metadata.get("creator")
            album = metadata.get("album")
            mb_trackid = metadata.get("mb_trackid") or metadata.get("mb_releasetrackid")

            if import_result.status in {"imported", "duplicate"}:
                final_path = import_result.final_path
                assert final_path is not None
                if import_result.status == "imported":
                    imported_count += 1
                    item_status = ItemStatus.IMPORTED
                else:
                    duplicate_count += 1
                    item_status = ItemStatus.SKIPPED_EXISTING
                self.db.update_job_item(
                    item.id,
                    status=item_status,
                    final_path=str(final_path),
                    mb_trackid=mb_trackid,
                )
                self.db.upsert_source_mapping(
                    source_key=source_key,
                    youtube_video_id=item.youtube_video_id,
                    playlist_item_id=item.playlist_item_id,
                    final_path=str(final_path),
                    mb_trackid=mb_trackid,
                    artist=artist,
                    album=album,
                    title=title,
                )
                track_paths.append(final_path)
                continue

            if import_result.status == "duplicate_ambiguous":
                ambiguous_count += 1
                self.db.update_job_item(
                    item.id,
                    status=ItemStatus.SKIPPED_AMBIGUOUS,
                    error_message="Duplicate candidates were ambiguous.",
                )
                continue

            failed_count += 1
            self.db.update_job_item(
                item.id,
                status=ItemStatus.FAILED,
                error_message=import_result.reason or "Import failed.",
            )

        playlist_path: Path | None = None
        if job.request_kind is RequestKind.PLAYLIST and track_paths:
            self.db.update_job(job.id, status=JobStatus.PLAYLIST_BUILDING)
            playlist_title = preflight.playlist_title or preflight.source_title or f"Playlist {job.id}"
            playlist_path = self.playlist_writer.write_playlist(
                playlist_title=playlist_title,
                playlist_id=preflight.source_id,
                track_paths=track_paths,
            )

        summary = (
            f"Imported: {imported_count}, duplicates: {duplicate_count}, failed: {failed_count}, "
            f"ambiguous duplicates: {ambiguous_count}"
        )
        if playlist_path:
            summary = f"{summary}, playlist: {playlist_path.name}"
        final_status = JobStatus.COMPLETED if failed_count == 0 and ambiguous_count == 0 else JobStatus.PARTIAL
        self.db.update_job(
            job.id,
            status=final_status,
            result_summary=summary,
            progress_percent=100.0,
            progress_eta_seconds=0,
            finished_at=self._timestamp(),
        )
        await self._notify(job.chat_id, f"Job #{job.id} finished.\n{summary}", job.id)

    async def _update_progress(
        self,
        job_id: int,
        percent: float | None,
        eta: int | None,
        speed: str | None,
    ) -> None:
        self.db.update_job(
            job_id,
            progress_percent=percent,
            progress_eta_seconds=eta,
            progress_speed=speed,
        )

    def _load_metadata(self, item: JobItemRecord, info_json_path: Path | None) -> dict[str, Any]:
        metadata = json.loads(item.metadata_json)
        if info_json_path and info_json_path.exists():
            try:
                info_metadata = json.loads(info_json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return metadata
            info_metadata.update({key: value for key, value in metadata.items() if value})
            return info_metadata
        return metadata

    def _cancel_remaining_items(self, job_id: int) -> None:
        for item in self.db.get_job_items(job_id):
            if item.status is ItemStatus.PENDING:
                self.db.update_job_item(item.id, status=ItemStatus.CANCELLED)

    async def _notify(self, chat_id: int, message: str, job_id: int | None) -> None:
        self.db.add_message(direction="outgoing", body=message, job_id=job_id, telegram_chat_id=chat_id)
        if self._notifier:
            await self._notifier(chat_id, message, job_id)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")
