from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import ImportResult


class BeetsError(RuntimeError):
    pass


class BeetsRunner:
    def __init__(self, config: AppConfig):
        self.config = config

    async def import_track(self, audio_path: Path, metadata: dict[str, Any]) -> ImportResult:
        max_id_before = self._get_max_item_id()
        process = await asyncio.create_subprocess_exec(
            "beet",
            "-c",
            str(self.config.runtime_beets_config_path),
            "import",
            "-s",
            "-q",
            str(audio_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            error = stderr_bytes.decode("utf-8", errors="replace").strip() or stdout_bytes.decode(
                "utf-8", errors="replace"
            ).strip()
            raise BeetsError(error or "beet import failed without an error message.")

        imported_path = self._find_newly_imported_path(max_id_before)
        if imported_path:
            return ImportResult(status="imported", final_path=imported_path)

        duplicate_path, reason = self.find_existing_path(metadata)
        if duplicate_path:
            return ImportResult(status="duplicate", final_path=duplicate_path, reason=reason)
        if reason == "ambiguous":
            return ImportResult(status="duplicate_ambiguous", reason=reason)
        return ImportResult(status="missing", reason="Imported file could not be resolved in beets.")

    def find_existing_path(self, metadata: dict[str, Any]) -> tuple[Path | None, str | None]:
        mb_trackid = metadata.get("mb_trackid") or metadata.get("mb_releasetrackid")
        with self._connect_library() as connection:
            if mb_trackid:
                rows = connection.execute(
                    "SELECT path FROM items WHERE mb_trackid = ? ORDER BY id DESC",
                    (mb_trackid,),
                ).fetchall()
                if len(rows) == 1:
                    return _path_from_value(rows[0]["path"]), "mb_trackid"
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
            return _path_from_value(rows[0]["path"]), "metadata"
        if len(rows) > 1:
            return None, "ambiguous"
        return None, None

    def _connect_library(self) -> sqlite3.Connection:
        self.config.beets_library_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.config.beets_library_path)
        connection.row_factory = sqlite3.Row
        connection.text_factory = lambda value: value.decode("utf-8", errors="replace")
        return connection

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
        return _path_from_value(row["path"]) if row else None


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _path_from_value(value: Any) -> Path:
    if isinstance(value, bytes):
        return Path(value.decode("utf-8", errors="replace"))
    return Path(str(value))
