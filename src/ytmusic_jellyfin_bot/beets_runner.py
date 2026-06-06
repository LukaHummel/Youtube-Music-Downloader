from __future__ import annotations

import asyncio
import logging
import shlex
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import requests
from mediafile import FileTypeError, Image, MediaFile, MutagenError, UnreadableFileError

from .config import AppConfig
from .metadata import normalize_track_metadata, tag_values_from_metadata
from .models import ImportResult

LOGGER = logging.getLogger(__name__)


class BeetsError(RuntimeError):
    pass


class BeetsRunner:
    def __init__(self, config: AppConfig):
        self.config = config

    async def import_track(self, audio_path: Path, metadata: dict[str, Any]) -> ImportResult:
        normalized_metadata = normalize_track_metadata(metadata)
        self.write_baseline_tags(audio_path, normalized_metadata, overwrite=True)
        max_id_before = self._get_max_item_id()
        command = [
            "beet",
            "-c",
            str(self.config.runtime_beets_config_path),
            "import",
            "-s",
            "-q",
            str(audio_path),
        ]
        LOGGER.info("Running beets import command: %s", _format_command(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        _log_beets_output(stdout=stdout, stderr=stderr, returncode=process.returncode)
        if process.returncode != 0:
            error = stderr or stdout
            raise BeetsError(error or "beet import failed without an error message.")

        imported_path = self._find_newly_imported_path(max_id_before)
        if imported_path:
            return ImportResult(status="imported", final_path=imported_path)

        duplicate_path, reason = self.find_existing_path(normalized_metadata)
        if duplicate_path:
            return ImportResult(status="duplicate", final_path=duplicate_path, reason=reason)
        if reason == "ambiguous":
            return ImportResult(status="duplicate_ambiguous", reason=reason)
        return ImportResult(status="missing", reason="Imported file could not be resolved in beets.")

    def write_baseline_tags(self, audio_path: Path, metadata: dict[str, Any], *, overwrite: bool) -> None:
        values = tag_values_from_metadata(metadata)
        if not values:
            LOGGER.warning("No fallback metadata was available: path=%s", audio_path)
            return
        artwork_url = values.pop("artwork_url", None)
        try:
            media = MediaFile(audio_path)
            changed_fields: list[str] = []
            for field, value in values.items():
                current_value = getattr(media, field, None)
                if overwrite or _missing_tag_value(current_value):
                    setattr(media, field, value)
                    changed_fields.append(field)
            if artwork_url and (overwrite or _missing_tag_value(media.images)):
                image = _download_artwork(
                    artwork_url,
                    timeout=getattr(self.config, "ytmusic_request_timeout", 10.0),
                )
                if image:
                    media.images = [image]
                    changed_fields.append("images")
            if not changed_fields:
                LOGGER.info(
                    "Fallback metadata already present: path=%s candidate_fields=%s",
                    audio_path,
                    ",".join(sorted([*values, "artwork_url"] if artwork_url else values)),
                )
                return
            media.save()
        except (FileTypeError, MutagenError, UnreadableFileError) as exc:
            LOGGER.warning(
                "Could not write fallback metadata: path=%s error=%s",
                audio_path,
                exc,
            )
            return
        LOGGER.info(
            "Wrote fallback metadata: path=%s overwrite=%s fields=%s",
            audio_path,
            overwrite,
            ",".join(sorted(changed_fields)),
        )

    def find_existing_path(self, metadata: dict[str, Any]) -> tuple[Path | None, str | None]:
        mb_trackid = metadata.get("mb_trackid") or metadata.get("mb_releasetrackid")
        with self._connect_library() as connection:
            if mb_trackid:
                rows = connection.execute(
                    "SELECT path FROM items WHERE mb_trackid = ? ORDER BY id DESC",
                    (mb_trackid,),
                ).fetchall()
                if len(rows) == 1:
                    return _library_path_from_value(rows[0]["path"], self.config.music_library_dir), "mb_trackid"
                if len(rows) > 1:
                    return None, "ambiguous"

            title = _normalize_text(metadata.get("track") or metadata.get("title"))
            artist = _normalize_text(metadata.get("artist") or metadata.get("albumartist") or metadata.get("creator"))
            album = _normalize_text(metadata.get("album")) or ""
            if not title or not artist:
                return None, None
            rows = connection.execute(
                """
                SELECT path FROM items
                WHERE lower(title) = ? AND lower(artist) = ? AND lower(COALESCE(album, '')) = ?
                ORDER BY id DESC
                """,
                (title, artist, album),
            ).fetchall()
        if len(rows) == 1:
            return _library_path_from_value(rows[0]["path"], self.config.music_library_dir), "metadata"
        if len(rows) > 1:
            return None, "ambiguous"
        return None, None

    @contextmanager
    def _connect_library(self) -> Iterator[sqlite3.Connection]:
        self.config.beets_library_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.config.beets_library_path)
        connection.row_factory = sqlite3.Row
        connection.text_factory = lambda value: value.decode("utf-8", errors="replace")
        try:
            yield connection
        finally:
            connection.close()

    def _get_max_item_id(self) -> int:
        if not self.config.beets_library_path.exists():
            return 0
        with self._connect_library() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()
        return int(row[0]) if row else 0

    def _find_newly_imported_path(self, max_id_before: int) -> Path | None:
        if not self.config.beets_library_path.exists():
            return None
        with self._connect_library() as connection:
            row = connection.execute(
                "SELECT path FROM items WHERE id > ? ORDER BY id DESC LIMIT 1",
                (max_id_before,),
            ).fetchone()
        return _library_path_from_value(row["path"], self.config.music_library_dir) if row else None


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _path_from_value(value: Any) -> Path:
    if isinstance(value, bytes):
        return Path(value.decode("utf-8", errors="replace"))
    return Path(str(value))


def _library_path_from_value(value: Any, music_library_dir: Path) -> Path:
    path = _path_from_value(value)
    if path.is_absolute():
        return path
    return music_library_dir / path


def _missing_tag_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (int, float)):
        return value <= 0
    if isinstance(value, (list, tuple, set)):
        return not value
    return False


def _download_artwork(url: str, *, timeout: float) -> Image | None:
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.warning("Could not download ytmusic artwork: %s", exc)
        return None
    if len(response.content) > 10 * 1024 * 1024:
        LOGGER.warning("Skipping ytmusic artwork because it is larger than 10 MiB")
        return None
    try:
        return Image(response.content)
    except (AssertionError, ValueError) as exc:
        LOGGER.warning("Could not parse ytmusic artwork: %s", exc)
        return None


def _format_command(command: list[str]) -> str:
    return " ".join(shlex.quote(piece) for piece in command)


def _log_beets_output(*, stdout: str, stderr: str, returncode: int | None) -> None:
    output = stderr or stdout
    if not output:
        LOGGER.info("beets import finished returncode=%s with no console output", returncode)
        return
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    summary = " | ".join(lines[-8:])
    if len(summary) > 1200:
        summary = f"{summary[:1197]}..."
    LOGGER.info("beets import finished returncode=%s output=%s", returncode, summary)
