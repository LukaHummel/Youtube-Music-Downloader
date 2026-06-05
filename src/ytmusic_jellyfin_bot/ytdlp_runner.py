from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import AppConfig
from .models import DownloadResult, PreflightItem, PreflightResult, RequestKind

ProgressCallback = Callable[[float | None, int | None, str | None], Awaitable[None]]

AUTH_ERROR_MARKERS = ("private video", "private playlist", "sign in", "cookies", "members-only")
PROGRESS_RE = re.compile(r"^download:(?P<percent>[^|]*)\|(?P<speed>[^|]*)\|(?P<eta>[^|]*)\|")
AUDIO_SUFFIXES = {".aac", ".m4a", ".mp3", ".opus", ".flac", ".wav", ".ogg", ".alac"}


class YtDlpError(RuntimeError):
    def __init__(self, message: str, *, auth_required: bool = False):
        super().__init__(message)
        self.auth_required = auth_required


class YtDlpRunner:
    def __init__(self, config: AppConfig):
        self.config = config

    async def preflight(self, url: str, request_kind: RequestKind) -> PreflightResult:
        stdout, stderr, returncode = await self._run_capture(
            [*self._base_command(), "--dump-single-json", "--skip-download", "--no-warnings", url]
        )
        if returncode != 0:
            raise YtDlpError(self._format_error(stderr or stdout))
        try:
            payload = json.loads(stdout.strip())
        except json.JSONDecodeError as exc:
            raise YtDlpError("yt-dlp did not return valid JSON during preflight.") from exc

        if request_kind is RequestKind.TRACK:
            if not self._is_music_like(payload):
                raise YtDlpError(
                    "This URL does not resolve to a clear YouTube Music track. Submit a music track or playlist URL."
                )
            item = self._build_item_from_entry(payload, 1, payload.get("id"))
            return PreflightResult(
                source_id=payload.get("id") or "unknown",
                source_title=payload.get("title"),
                playlist_title=None,
                items=[item],
            )

        entries = payload.get("entries") or []
        items = [
            self._build_item_from_entry(entry, index, payload.get("id"))
            for index, entry in enumerate(entries, start=1)
            if isinstance(entry, dict) and entry.get("id")
        ]
        if not items:
            raise YtDlpError("No downloadable tracks were found in this playlist.")
        return PreflightResult(
            source_id=payload.get("id") or "unknown",
            source_title=payload.get("title"),
            playlist_title=payload.get("title"),
            items=items,
        )

    async def download_track(
        self,
        *,
        job_id: int,
        item_index: int,
        url: str,
        progress_callback: ProgressCallback,
    ) -> DownloadResult:
        item_dir = self.config.staging_dir / str(job_id) / f"{item_index:04d}"
        item_dir.mkdir(parents=True, exist_ok=True)
        command = [
            *self._base_command(),
            "--download-archive",
            str(self.config.ytdlp_archive_path),
            "--newline",
            "--progress-template",
            "download:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(info.id)s|%(info.title)s",
            "--paths",
            str(item_dir),
            "--output",
            "%(album,artist,playlist_title,creator)s/%(track,title)s.%(ext)s",
            url,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        collected: list[str] = []
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            collected.append(text)
            match = PROGRESS_RE.match(text)
            if match:
                await progress_callback(
                    _parse_percent(match.group("percent")),
                    _parse_eta(match.group("eta")),
                    _clean_progress_value(match.group("speed")),
                )
        returncode = await process.wait()
        if returncode != 0:
            output = "\n".join(collected)
            raise YtDlpError(self._format_error(output), auth_required=_contains_auth_marker(output))

        audio_candidates = sorted(
            (
                path
                for path in item_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
            ),
            key=lambda path: len(path.parts),
        )
        if not audio_candidates:
            raise YtDlpError("yt-dlp finished without producing an audio file.")
        info_json_path = next((path for path in item_dir.rglob("*.info.json") if path.is_file()), None)
        return DownloadResult(audio_path=audio_candidates[0], info_json_path=info_json_path)

    def _base_command(self) -> list[str]:
        command = ["yt-dlp", "--config-locations", str(self.config.runtime_ytdlp_config_path)]
        if self.config.cookies_available:
            command.extend(["--cookies", str(self.config.ytdlp_cookies_file)])
        return command

    async def _run_capture(self, command: list[str]) -> tuple[str, str, int]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        returncode = process.returncode
        if returncode is None:
            raise YtDlpError("yt-dlp exited without a return code.")
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        error_output = stderr or stdout
        if returncode != 0 and _contains_auth_marker(error_output):
            raise YtDlpError(self._format_error(error_output), auth_required=True)
        return stdout, stderr, returncode

    @staticmethod
    def _build_item_from_entry(entry: dict[str, Any], index: int, playlist_id: str | None) -> PreflightItem:
        video_id = entry["id"]
        normalized_url = f"https://music.youtube.com/watch?v={video_id}"
        return PreflightItem(
            item_index=index,
            source_url=normalized_url,
            normalized_url=normalized_url,
            youtube_video_id=video_id,
            playlist_item_id=f"{playlist_id}:{index}" if playlist_id else None,
            title=entry.get("track") or entry.get("title"),
            artist=_coalesce_artist(entry),
            album=entry.get("album"),
            metadata=entry,
        )

    @staticmethod
    def _is_music_like(payload: dict[str, Any]) -> bool:
        return bool(payload.get("track") or payload.get("album") or payload.get("artist") or payload.get("artists"))

    @staticmethod
    def _format_error(output: str) -> str:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return lines[-1] if lines else "yt-dlp failed without an error message."


def _parse_percent(value: str) -> float | None:
    cleaned = _clean_progress_value(value)
    if not cleaned:
        return None
    cleaned = cleaned.removesuffix("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_eta(value: str) -> int | None:
    cleaned = _clean_progress_value(value)
    if not cleaned or cleaned in {"NA", "--:--"}:
        return None
    return int(cleaned) if cleaned.isdigit() else None


def _clean_progress_value(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned if cleaned and cleaned != "NA" else None


def _coalesce_artist(payload: dict[str, Any]) -> str | None:
    artists = payload.get("artists")
    if isinstance(artists, list) and artists:
        names: list[str] = []
        for artist in artists:
            if isinstance(artist, dict) and artist.get("name"):
                names.append(str(artist["name"]))
            elif artist:
                names.append(str(artist))
        if names:
            return ", ".join(names)
    return payload.get("artist") or payload.get("creator") or payload.get("channel")


def _contains_auth_marker(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)
