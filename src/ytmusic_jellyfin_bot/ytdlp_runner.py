from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import AppConfig, DEFAULT_YOUTUBE_PLAYER_CLIENTS
from .metadata import normalize_track_metadata
from .models import DownloadResult, PreflightItem, PreflightResult, RequestKind

ProgressCallback = Callable[[float | None, int | None, str | None], Awaitable[None]]

LOGGER = logging.getLogger(__name__)

AUTH_ERROR_MARKERS = (
    "private video",
    "private playlist",
    "members-only",
    "sign in",
    "use --cookies",
    "cookies are required",
    "cookie file",
    "not a bot",
    "confirm your age",
    "age-restricted",
)
PROGRESS_RE = re.compile(r"^\s*download:(?P<percent>[^|]*)\|(?P<speed>[^|]*)\|(?P<eta>[^|]*)\|")
AUDIO_SUFFIXES = {".aac", ".m4a", ".mp3", ".opus", ".flac", ".wav", ".ogg", ".alac"}


@dataclass(frozen=True, slots=True)
class FormatSelection:
    format_id: str
    player_client: str | None
    ext: str | None
    acodec: str | None
    vcodec: str | None
    abr: float | None
    tbr: float | None


class YtDlpError(RuntimeError):
    def __init__(self, message: str, *, auth_required: bool = False, output: str | None = None):
        super().__init__(message)
        self.auth_required = auth_required
        self.output = output


class YtDlpRunner:
    def __init__(self, config: AppConfig):
        self.config = config

    async def preflight(self, url: str, request_kind: RequestKind) -> PreflightResult:
        command = [
            *self._preflight_command(),
            "--dump-single-json",
            "--skip-download",
            url,
        ]
        LOGGER.info("Running yt-dlp preflight command: %s", _format_command(command))
        stdout, stderr, returncode = await self._run_capture(command)
        if returncode != 0:
            output = stderr or stdout
            raise YtDlpError(
                self._format_error(output),
                auth_required=_contains_auth_marker(output),
                output=output,
            )
        _log_ytdlp_diagnostics("preflight", stderr)
        return self._parse_preflight_stdout(stdout, stderr, request_kind)

    def _parse_preflight_stdout(
        self,
        stdout: str,
        stderr: str,
        request_kind: RequestKind,
    ) -> PreflightResult:
        try:
            payload = json.loads(stdout.strip())
        except json.JSONDecodeError as exc:
            raise YtDlpError(
                "yt-dlp did not return valid JSON during preflight.",
                output=stderr or stdout,
            ) from exc

        if request_kind is RequestKind.TRACK:
            if not payload.get("id"):
                raise YtDlpError("This URL does not resolve to a downloadable YouTube video.")
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
        selection = await self._resolve_download_format(url)
        LOGGER.info(
            "Selected yt-dlp format for job_id=%s item_index=%s player_client=%s format_id=%s ext=%s "
            "acodec=%s vcodec=%s abr=%s tbr=%s",
            job_id,
            item_index,
            selection.player_client or "default",
            selection.format_id,
            selection.ext or "unknown",
            selection.acodec or "unknown",
            selection.vcodec or "unknown",
            selection.abr if selection.abr is not None else "unknown",
            selection.tbr if selection.tbr is not None else "unknown",
        )
        command = self._download_command(
            item_dir=item_dir,
            url=url,
            selection=selection,
        )
        LOGGER.info(
            "Running yt-dlp download command for job_id=%s item_index=%s: %s",
            job_id,
            item_index,
            _format_command(command),
        )
        returncode, collected = await self._run_download_process(
            command=command,
            job_id=job_id,
            item_index=item_index,
            progress_callback=progress_callback,
        )
        output = "\n".join(collected)
        if returncode != 0:
            raise YtDlpError(
                self._format_error(output),
                auth_required=_contains_auth_marker(output),
                output=output,
            )

        audio_candidates = sorted(
            (
                path
                for path in item_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
            ),
            key=lambda path: len(path.parts),
        )
        if not audio_candidates:
            raise YtDlpError(
                "yt-dlp finished without producing an audio file.",
                output="\n".join(collected),
            )
        info_json_path = next((path for path in item_dir.rglob("*.info.json") if path.is_file()), None)
        return DownloadResult(audio_path=audio_candidates[0], info_json_path=info_json_path)

    async def _resolve_download_format(self, url: str) -> FormatSelection:
        last_error: YtDlpError | None = None
        last_probe_output: str | None = None
        for player_client in self._download_player_clients():
            command = self._format_probe_command(url=url, player_client=player_client)
            LOGGER.info(
                "Resolving yt-dlp formats player_client=%s: %s",
                player_client or "default",
                _format_command(command),
            )
            stdout, stderr, returncode = await self._run_capture(command)
            if returncode != 0:
                output = stderr or stdout
                last_error = YtDlpError(
                    self._format_error(output),
                    auth_required=_contains_auth_marker(output),
                    output=output,
                )
                LOGGER.warning(
                    "yt-dlp format probe failed player_client=%s: %s",
                    player_client or "default",
                    str(last_error),
                )
                continue
            try:
                payload = json.loads(stdout.strip())
            except json.JSONDecodeError as exc:
                last_error = YtDlpError(
                    "yt-dlp did not return valid JSON while resolving formats.",
                    output=stderr or stdout,
                )
                raise last_error from exc

            selection = _select_best_audio_format(payload.get("formats") or [], player_client)
            if selection:
                _log_ytdlp_diagnostics("format probe", stderr)
                return selection
            last_probe_output = _probe_failure_details(stderr=stderr, payload=payload, player_client=player_client)
            LOGGER.warning(
                "yt-dlp format probe found no audio-capable formats for player_client=%s diagnostics=%s",
                player_client or "default",
                _summarize_output(last_probe_output),
            )

        if last_error:
            raise last_error
        raise YtDlpError(
            "yt-dlp did not find any audio-capable formats for this URL with the mounted cookies file. "
            "Check the logged yt-dlp diagnostics, refresh cookies, or configure a YouTube PO token if "
            "YouTube is enforcing one for this session.",
            auth_required=_contains_auth_marker(last_probe_output or ""),
            output=last_probe_output,
        )

    def _download_player_clients(self) -> tuple[str | None, ...]:
        raw_clients = getattr(self.config, "youtube_player_clients", DEFAULT_YOUTUBE_PLAYER_CLIENTS)
        return tuple(None if client.lower() == "default" else client for client in raw_clients)

    def _format_probe_command(self, *, url: str, player_client: str | None) -> list[str]:
        command = [
            "yt-dlp",
            "--ignore-config",
            "--cookies",
            str(self._require_cookies_file()),
            "--ignore-no-formats-error",
        ]
        extractor_args = self._youtube_extractor_args(player_client)
        if extractor_args:
            command.extend(["--extractor-args", extractor_args])
        command.extend(["--dump-single-json", "--skip-download", url])
        return command

    def _download_command(self, *, item_dir: Path, url: str, selection: FormatSelection) -> list[str]:
        command = [*self._base_command()]
        extractor_args = self._youtube_extractor_args(selection.player_client)
        if extractor_args:
            command.extend(["--extractor-args", extractor_args])
        command.extend(
            [
                "--format",
                selection.format_id,
                "--download-archive",
                str(self.config.ytdlp_archive_path),
                "--newline",
                "--progress-template",
                "download:download:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(info.id)s|%(info.title)s",
                "--paths",
                str(item_dir),
                "--output",
                "%(album,artist,playlist_title,creator)s/%(track,title)s.%(ext)s",
                url,
            ]
        )
        return command

    async def _run_download_process(
        self,
        *,
        command: list[str],
        job_id: int,
        item_index: int,
        progress_callback: ProgressCallback,
    ) -> tuple[int, list[str]]:
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
            LOGGER.debug("yt-dlp job_id=%s item_index=%s output: %s", job_id, item_index, text)
            match = PROGRESS_RE.match(text)
            if match:
                await progress_callback(
                    _parse_percent(match.group("percent")),
                    _parse_eta(match.group("eta")),
                    _clean_progress_value(match.group("speed")),
                )
        return await process.wait(), collected

    def _base_command(self) -> list[str]:
        command = ["yt-dlp", "--config-locations", str(self.config.runtime_ytdlp_config_path)]
        command.extend(["--cookies", str(self._require_cookies_file())])
        return command

    def _youtube_extractor_args(self, player_client: str | None) -> str | None:
        pieces: list[str] = []
        configured = getattr(self.config, "youtube_extractor_args", None)
        configured_args = configured.removeprefix("youtube:") if configured else None
        if player_client and not _has_configured_player_client(configured_args):
            pieces.append(f"player_client={player_client}")
        if configured:
            pieces.append(configured_args or "")
        if not pieces:
            return None
        return f"youtube:{';'.join(pieces)}"

    def _preflight_command(self) -> list[str]:
        command = [
            "yt-dlp",
            "--ignore-config",
            "--ignore-no-formats-error",
        ]
        command.extend(["--cookies", str(self._require_cookies_file())])
        return command

    def _require_cookies_file(self) -> Path:
        if not self.config.cookies_available:
            raise YtDlpError(
                f"Required cookies.txt file is not mounted or readable: {self.config.ytdlp_cookies_file}",
                auth_required=True,
            )
        return self.config.ytdlp_cookies_file

    async def _run_capture(self, command: list[str]) -> tuple[str, str, int]:
        LOGGER.debug("Running yt-dlp command: %s", _format_command(command))
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
        LOGGER.debug(
            "yt-dlp command finished returncode=%s stdout_bytes=%s stderr_bytes=%s",
            returncode,
            len(stdout_bytes),
            len(stderr_bytes),
        )
        return stdout, stderr, returncode

    @staticmethod
    def _build_item_from_entry(entry: dict[str, Any], index: int, playlist_id: str | None) -> PreflightItem:
        metadata = normalize_track_metadata(entry)
        video_id = entry["id"]
        normalized_url = f"https://music.youtube.com/watch?v={video_id}"
        return PreflightItem(
            item_index=index,
            source_url=normalized_url,
            normalized_url=normalized_url,
            youtube_video_id=video_id,
            playlist_item_id=f"{playlist_id}:{index}" if playlist_id else None,
            title=metadata.get("track") or metadata.get("title"),
            artist=metadata.get("artist") or _coalesce_artist(entry),
            album=metadata.get("album"),
            metadata=metadata,
        )

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
    if cleaned.isdigit():
        return int(cleaned)
    pieces = cleaned.split(":")
    if not 2 <= len(pieces) <= 3:
        return None
    if not all(piece.isdigit() for piece in pieces):
        return None
    seconds = 0
    for piece in pieces:
        seconds = seconds * 60 + int(piece)
    return seconds


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


def _format_command(command: list[str]) -> str:
    pieces: list[str] = []
    redact_next = False
    for piece in command:
        if redact_next:
            pieces.append(shlex.quote(_redact_extractor_args(piece)))
            redact_next = False
            continue
        pieces.append(shlex.quote(piece))
        if piece == "--extractor-args":
            redact_next = True
    return " ".join(pieces)


def _redact_extractor_args(value: str) -> str:
    return re.sub(r"(po_token=)[^;\s]+", r"\1<redacted>", value)


def _has_configured_player_client(value: str | None) -> bool:
    return bool(value and re.search(r"(?:^|[;,])player[-_]client=", value))


def _log_ytdlp_diagnostics(context: str, stderr: str) -> None:
    if not stderr.strip():
        return
    LOGGER.warning("yt-dlp %s diagnostics: %s", context, _summarize_output(stderr))


def _summarize_output(output: str | None, *, max_length: int = 1200) -> str:
    if not output:
        return "none"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "none"
    summary = " | ".join(lines[-8:])
    if len(summary) <= max_length:
        return _redact_extractor_args(summary)
    return _redact_extractor_args(f"{summary[:max_length - 3]}...")


def _probe_failure_details(
    *,
    stderr: str,
    payload: dict[str, Any],
    player_client: str | None,
) -> str:
    formats = payload.get("formats") or []
    requested_formats = payload.get("requested_formats") or []
    details = [
        f"player_client={player_client or 'default'}",
        f"format_count={len(formats)}",
        f"requested_format_count={len(requested_formats)}",
    ]
    if stderr.strip():
        details.append(stderr.strip())
    return "\n".join(details)


def _select_best_audio_format(
    formats: list[dict[str, Any]],
    player_client: str | None,
) -> FormatSelection | None:
    candidates = [candidate for candidate in formats if _has_audio(candidate)]
    if not candidates:
        return None

    audio_only = [candidate for candidate in candidates if candidate.get("vcodec") == "none"]
    selected = max(audio_only or candidates, key=_format_sort_key)
    format_id = selected.get("format_id")
    if not format_id:
        return None
    return FormatSelection(
        format_id=str(format_id),
        player_client=player_client,
        ext=_optional_str(selected.get("ext")),
        acodec=_optional_str(selected.get("acodec")),
        vcodec=_optional_str(selected.get("vcodec")),
        abr=_optional_float(selected.get("abr")),
        tbr=_optional_float(selected.get("tbr")),
    )


def _has_audio(candidate: dict[str, Any]) -> bool:
    acodec = candidate.get("acodec")
    return bool(acodec and acodec != "none")


def _format_sort_key(candidate: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        _optional_float(candidate.get("abr")) or 0.0,
        _optional_float(candidate.get("tbr")) or 0.0,
        _optional_float(candidate.get("asr")) or 0.0,
        _optional_float(candidate.get("filesize") or candidate.get("filesize_approx")) or 0.0,
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
