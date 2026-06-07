from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .beets_runner import BeetsError, BeetsRunner
from .config import AppConfig
from .db import Database
from .metadata import normalize_track_metadata
from .models import ItemStatus, JobItemRecord, JobRecord, JobStatus, RequestKind
from .playlist_writer import PlaylistWriter
from .ytdlp_runner import YtDlpError, YtDlpRunner
from .ytmusic_metadata import YtMusicMetadataProvider

Notifier = Callable[[int, str, int | None], Awaitable[None]]
ProgressNotifier = Callable[[int, bool], Awaitable[None]]

LOGGER = logging.getLogger(__name__)
MAX_ERROR_DETAIL_LENGTH = 1500


class JobWorker:
    def __init__(
        self,
        *,
        config: AppConfig,
        db: Database,
        ytdlp: YtDlpRunner,
        beets: BeetsRunner,
        playlist_writer: PlaylistWriter,
        ytmusic: YtMusicMetadataProvider | None = None,
    ):
        self.config = config
        self.db = db
        self.ytdlp = ytdlp
        self.beets = beets
        self.playlist_writer = playlist_writer
        self.ytmusic = ytmusic
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._stopping = False
        self._notifier: Notifier | None = None
        self._progress_notifier: ProgressNotifier | None = None

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def set_progress_notifier(self, notifier: ProgressNotifier) -> None:
        self._progress_notifier = notifier

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop(), name="job-worker")
            LOGGER.info("Job worker started")
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
            LOGGER.info("Job worker stopped")

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
                await self._notify_progress(job.id, True)

    async def _process_job(self, job: JobRecord) -> None:
        LOGGER.info(
            "Job #%s started: type=%s requested_by=%s url=%s",
            job.id,
            job.request_kind,
            job.requested_by or "unknown",
            job.normalized_url,
        )
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
        await self._notify_progress(job.id, True)
        self.db.update_job(job.id, status=JobStatus.PREFLIGHT)
        await self._notify_progress(job.id, True)
        LOGGER.info("Job #%s preflight started", job.id)

        try:
            preflight = await self.ytdlp.preflight(job.normalized_url, job.request_kind)
        except YtDlpError as exc:
            message = (
                "YouTube requires authentication for this request. This can happen with private or "
                "restricted content, age gates, or YouTube bot checks. Mount or refresh cookies.txt."
                if exc.auth_required
                else str(exc)
            )
            LOGGER.error(
                "Job #%s preflight failed: auth_required=%s message=%s details=%s",
                job.id,
                exc.auth_required,
                str(exc),
                _error_details(exc),
            )
            self.db.update_job(
                job.id,
                status=JobStatus.FAILED,
                error_message=message,
                finished_at=self._timestamp(),
            )
            await self._notify_progress(job.id, True)
            return

        if self.ytmusic:
            LOGGER.info(
                "Job #%s ytmusic metadata enrichment started: enabled=%s",
                job.id,
                getattr(self.config, "ytmusic_metadata_enabled", True),
            )
            preflight = await self.ytmusic.enrich_preflight(preflight)
            LOGGER.info("Job #%s ytmusic metadata enrichment completed", job.id)

        self.db.replace_job_items(job.id, preflight.items)
        LOGGER.info(
            "Job #%s preflight completed: source_id=%s title=%s items=%s",
            job.id,
            preflight.source_id,
            preflight.source_title or "unknown",
            len(preflight.items),
        )
        self.db.update_job(
            job.id,
            source_id=preflight.source_id,
            source_title=preflight.source_title,
            playlist_title=preflight.playlist_title,
            total_items=len(preflight.items),
        )
        await self._notify_progress(job.id, True)

        track_paths: list[Path] = []
        imported_count = 0
        duplicate_count = 0
        failed_count = 0
        ambiguous_count = 0

        for item in self.db.get_job_items(job.id):
            if self.db.is_cancel_requested(job.id):
                LOGGER.warning("Job #%s cancellation requested at item %s", job.id, item.item_index)
                self._cancel_remaining_items(job.id)
                self.db.update_job(
                    job.id,
                    status=JobStatus.CANCELLED,
                    result_summary=f"Cancelled after {imported_count} imported items.",
                    finished_at=self._timestamp(),
                )
                await self._notify_progress(job.id, True)
                return

            source_key = self.db.source_key_for_video(item.youtube_video_id or "")
            mapping = self.db.find_source_mapping(source_key)
            mapped_final_path = self._library_path_from_value(mapping["final_path"]) if mapping else None
            if mapped_final_path and mapped_final_path.exists():
                duplicate_count += 1
                final_path = mapped_final_path
                self.beets.write_baseline_tags(final_path, self._load_metadata(item, None), overwrite=False)
                LOGGER.info(
                    "Job #%s item %s skipped existing file: video_id=%s path=%s",
                    job.id,
                    item.item_index,
                    item.youtube_video_id or "unknown",
                    final_path,
                )
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
                await self._notify_progress(job.id, True)
                track_paths.append(final_path)
                continue

            self.db.update_job(
                job.id,
                status=JobStatus.DOWNLOADING,
                current_item_index=item.item_index,
            )
            self.db.update_job_item(item.id, status=ItemStatus.DOWNLOADING)
            await self._notify_progress(job.id, True)
            LOGGER.info(
                "Job #%s item %s/%s download started: video_id=%s title=%s",
                job.id,
                item.item_index,
                len(preflight.items),
                item.youtube_video_id or "unknown",
                item.title or "unknown",
            )

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
                    "YouTube requires authentication for this download. Refresh the mounted cookies.txt file."
                    if exc.auth_required
                    else str(exc)
                )
                LOGGER.warning(
                    "Job #%s item %s download failed: auth_required=%s message=%s details=%s",
                    job.id,
                    item.item_index,
                    exc.auth_required,
                    str(exc),
                    _error_details(exc),
                )
                self.db.update_job_item(item.id, status=ItemStatus.FAILED, error_message=failure_message)
                await self._notify_progress(job.id, True)
                continue

            self.db.update_job(job.id, status=JobStatus.RETAGGING)
            LOGGER.info(
                "Job #%s item %s download completed: path=%s",
                job.id,
                item.item_index,
                download_result.audio_path,
            )
            self.db.update_job_item(
                item.id,
                status=ItemStatus.RETAGGING,
                download_path=str(download_result.audio_path),
            )
            await self._notify_progress(job.id, True)
            metadata = self._load_metadata(item, download_result.info_json_path)

            try:
                import_result = await self.beets.import_track(download_result.audio_path, metadata)
            except BeetsError as exc:
                failed_count += 1
                LOGGER.warning(
                    "Job #%s item %s beets import failed: %s",
                    job.id,
                    item.item_index,
                    str(exc),
                )
                self.db.update_job_item(item.id, status=ItemStatus.FAILED, error_message=str(exc))
                await self._notify_progress(job.id, True)
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
                    LOGGER.info(
                        "Job #%s item %s imported: path=%s",
                        job.id,
                        item.item_index,
                        final_path,
                    )
                else:
                    duplicate_count += 1
                    item_status = ItemStatus.SKIPPED_EXISTING
                    LOGGER.info(
                        "Job #%s item %s matched existing beets item: path=%s reason=%s",
                        job.id,
                        item.item_index,
                        final_path,
                        import_result.reason or "duplicate",
                    )
                self.db.update_job_item(
                    item.id,
                    status=item_status,
                    final_path=str(final_path),
                    mb_trackid=mb_trackid,
                )
                await self._notify_progress(job.id, True)
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
                self.beets.write_baseline_tags(final_path, metadata, overwrite=False)
                self._cleanup_download_folder(
                    job_id=job.id,
                    item_index=item.item_index,
                    audio_path=download_result.audio_path,
                    final_path=final_path,
                )
                track_paths.append(final_path)
                continue

            if import_result.status == "duplicate_ambiguous":
                ambiguous_count += 1
                LOGGER.warning(
                    "Job #%s item %s skipped due to ambiguous duplicate candidates",
                    job.id,
                    item.item_index,
                )
                self.db.update_job_item(
                    item.id,
                    status=ItemStatus.SKIPPED_AMBIGUOUS,
                    error_message="Duplicate candidates were ambiguous.",
                )
                await self._notify_progress(job.id, True)
                continue

            failed_count += 1
            LOGGER.warning(
                "Job #%s item %s import did not resolve: status=%s reason=%s",
                job.id,
                item.item_index,
                import_result.status,
                import_result.reason or "unknown",
            )
            self.db.update_job_item(
                item.id,
                status=ItemStatus.FAILED,
                error_message=import_result.reason or "Import failed.",
            )
            await self._notify_progress(job.id, True)

        playlist_path: Path | None = None
        if job.request_kind is RequestKind.PLAYLIST and track_paths:
            self.db.update_job(job.id, status=JobStatus.PLAYLIST_BUILDING)
            await self._notify_progress(job.id, True)
            playlist_title = preflight.playlist_title or preflight.source_title or f"Playlist {job.id}"
            playlist_path = self.playlist_writer.write_playlist(
                playlist_title=playlist_title,
                playlist_id=preflight.source_id,
                track_paths=track_paths,
            )
            LOGGER.info("Job #%s playlist written: path=%s", job.id, playlist_path)

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
        await self._notify_progress(job.id, True)
        log_method = LOGGER.info if final_status is JobStatus.COMPLETED else LOGGER.warning
        log_method("Job #%s finished: status=%s %s", job.id, final_status, summary)

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
        await self._notify_progress(job_id, False)

    def _load_metadata(self, item: JobItemRecord, info_json_path: Path | None) -> dict[str, Any]:
        metadata = json.loads(item.metadata_json)
        if info_json_path and info_json_path.exists():
            try:
                info_metadata = json.loads(info_json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                LOGGER.warning("Ignoring invalid info.json for job_item_id=%s path=%s", item.id, info_json_path)
                return normalize_track_metadata(metadata)
            info_metadata.update({key: value for key, value in metadata.items() if value})
            return normalize_track_metadata(info_metadata)
        return normalize_track_metadata(metadata)

    def _cancel_remaining_items(self, job_id: int) -> None:
        for item in self.db.get_job_items(job_id):
            if item.status is ItemStatus.PENDING:
                self.db.update_job_item(item.id, status=ItemStatus.CANCELLED)

    def _library_path_from_value(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return self.config.music_library_dir / path

    def _cleanup_download_folder(
        self,
        *,
        job_id: int,
        item_index: int,
        audio_path: Path,
        final_path: Path,
    ) -> None:
        cleanup_paths = self._download_cleanup_paths(job_id, item_index, audio_path)
        if cleanup_paths is None:
            return
        item_dir, staging_dir = cleanup_paths
        final_resolved = final_path.resolve(strict=False)
        if final_path.exists() and final_resolved.is_relative_to(item_dir):
            LOGGER.warning(
                "Skipping temporary download cleanup because the final path is inside the staging folder: "
                "item_dir=%s final_path=%s",
                item_dir,
                final_path,
            )
            return

        try:
            shutil.rmtree(item_dir)
        except FileNotFoundError:
            LOGGER.debug("Temporary download folder was already removed: path=%s", item_dir)
            return
        except OSError as exc:
            LOGGER.warning("Could not clean up temporary download folder: path=%s error=%s", item_dir, exc)
            return

        LOGGER.info("Cleaned up temporary download folder: path=%s", item_dir)
        self._remove_empty_download_parents(item_dir.parent, staging_dir)

    def _download_cleanup_paths(
        self,
        job_id: int,
        item_index: int,
        audio_path: Path,
    ) -> tuple[Path, Path] | None:
        staging_value = getattr(self.config, "staging_dir", None)
        if not staging_value:
            return None

        try:
            staging_dir = Path(staging_value).resolve(strict=False)
            item_dir = (staging_dir / str(job_id) / f"{item_index:04d}").resolve(strict=False)
            audio_resolved = audio_path.resolve(strict=False)
        except OSError as exc:
            LOGGER.warning("Could not resolve temporary download cleanup paths: %s", exc)
            return None

        if not item_dir.is_relative_to(staging_dir):
            LOGGER.warning(
                "Skipping temporary download cleanup because the item folder is outside staging: "
                "staging_dir=%s item_dir=%s",
                staging_dir,
                item_dir,
            )
            return None
        if not audio_resolved.is_relative_to(item_dir):
            LOGGER.warning(
                "Skipping temporary download cleanup because the audio file is outside the expected item folder: "
                "item_dir=%s audio_path=%s",
                item_dir,
                audio_path,
            )
            return None
        return item_dir, staging_dir

    def _remove_empty_download_parents(self, start: Path, staging_dir: Path) -> None:
        current = start
        while current != staging_dir and current.is_relative_to(staging_dir):
            try:
                current.rmdir()
            except FileNotFoundError:
                current = current.parent
                continue
            except OSError:
                break
            LOGGER.debug("Removed empty temporary download parent folder: path=%s", current)
            current = current.parent

    async def _notify(self, chat_id: int, message: str, job_id: int | None) -> None:
        self.db.add_message(direction="outgoing", body=message, job_id=job_id, telegram_chat_id=chat_id)
        if self._notifier:
            LOGGER.debug("Sending Telegram notification for job_id=%s chat_id=%s", job_id, chat_id)
            await self._notifier(chat_id, message, job_id)

    async def _notify_progress(self, job_id: int, force: bool) -> None:
        if not self._progress_notifier:
            return
        try:
            await self._progress_notifier(job_id, force)
        except Exception:
            LOGGER.exception("Telegram progress notification failed for job_id=%s", job_id)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")


def _error_details(exc: YtDlpError) -> str:
    output = exc.output
    if not output:
        return str(exc)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return str(exc)
    details = " | ".join(lines[-5:])
    if len(details) <= MAX_ERROR_DETAIL_LENGTH:
        return details
    return f"{details[:MAX_ERROR_DETAIL_LENGTH - 3]}..."
