from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class RequestKind(StrEnum):
    TRACK = "track"
    PLAYLIST = "playlist"


class JobStatus(StrEnum):
    QUEUED = "queued"
    NORMALIZING = "normalizing"
    PREFLIGHT = "preflight"
    DOWNLOADING = "downloading"
    RETAGGING = "retagging"
    PLAYLIST_BUILDING = "playlist_building"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemStatus(StrEnum):
    PENDING = "pending"
    SKIPPED_EXISTING = "skipped_existing"
    DOWNLOADING = "downloading"
    RETAGGING = "retagging"
    IMPORTED = "imported"
    FAILED = "failed"
    SKIPPED_AMBIGUOUS = "skipped_ambiguous"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class NormalizedRequest:
    source_url: str
    normalized_url: str
    request_kind: RequestKind
    youtube_video_id: str | None = None
    playlist_id: str | None = None


@dataclass(slots=True)
class PreflightItem:
    item_index: int
    source_url: str
    normalized_url: str
    youtube_video_id: str
    playlist_item_id: str | None
    title: str | None
    artist: str | None
    album: str | None
    metadata: dict[str, Any]


@dataclass(slots=True)
class PreflightResult:
    source_id: str
    source_title: str | None
    playlist_title: str | None
    items: list[PreflightItem]


@dataclass(slots=True)
class JobRecord:
    id: int
    source_url: str
    normalized_url: str
    request_kind: RequestKind
    status: JobStatus
    chat_id: int
    user_id: int
    requested_by: str | None
    source_id: str | None
    source_title: str | None
    playlist_title: str | None
    current_item_index: int
    total_items: int
    progress_percent: float | None
    progress_eta_seconds: int | None
    progress_speed: str | None
    progress_message_id: int | None
    error_message: str | None
    result_summary: str | None
    cancel_requested: bool
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(slots=True)
class JobItemRecord:
    id: int
    job_id: int
    item_index: int
    source_url: str
    normalized_url: str
    youtube_video_id: str | None
    playlist_item_id: str | None
    title: str | None
    artist: str | None
    album: str | None
    status: ItemStatus
    download_path: str | None
    final_path: str | None
    error_message: str | None
    mb_trackid: str | None
    metadata_json: str


@dataclass(slots=True)
class DownloadResult:
    audio_path: Path
    info_json_path: Path | None


@dataclass(slots=True)
class ImportResult:
    status: str
    final_path: Path | None = None
    reason: str | None = None
