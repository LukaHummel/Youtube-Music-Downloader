from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_allowed_ids(raw_value: str) -> frozenset[int]:
    ids: set[int] = set()
    for piece in raw_value.split(","):
        value = piece.strip()
        if value:
            ids.add(int(value))
    return frozenset(ids)


@dataclass(slots=True)
class AppConfig:
    telegram_bot_token: str
    allowed_telegram_ids: frozenset[int]
    music_library_dir: Path
    staging_dir: Path
    app_state_dir: Path
    ytdlp_cookies_file: Path
    worker_concurrency: int
    log_level: str
    project_root: Path

    @classmethod
    def from_env(cls) -> "AppConfig":
        project_root = Path(__file__).resolve().parents[2]
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        raw_ids = os.environ.get("ALLOWED_TELEGRAM_IDS", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not raw_ids:
            raise ValueError("ALLOWED_TELEGRAM_IDS is required")
        return cls(
            telegram_bot_token=token,
            allowed_telegram_ids=_parse_allowed_ids(raw_ids),
            music_library_dir=Path(os.environ.get("MUSIC_LIBRARY_DIR", "/music")).resolve(),
            staging_dir=Path(os.environ.get("STAGING_DIR", "/downloads")).resolve(),
            app_state_dir=Path(os.environ.get("APP_STATE_DIR", "/data")).resolve(),
            ytdlp_cookies_file=Path(
                os.environ.get("YTDLP_COOKIES_FILE", "/run/secrets/youtube_cookies.txt")
            ).resolve(),
            worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", "1")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            project_root=project_root,
        )

    @property
    def db_path(self) -> Path:
        return self.app_state_dir / "app.db"

    @property
    def logs_dir(self) -> Path:
        return self.app_state_dir / "logs"

    @property
    def runtime_config_dir(self) -> Path:
        return self.app_state_dir / "runtime"

    @property
    def playlist_dir(self) -> Path:
        return self.music_library_dir / "Playlists"

    @property
    def beets_library_path(self) -> Path:
        return self.app_state_dir / "beets" / "musiclibrary.db"

    @property
    def ytdlp_archive_path(self) -> Path:
        return self.app_state_dir / "yt-dlp-archive.txt"

    @property
    def template_beets_config_path(self) -> Path:
        return self.project_root / "config" / "beets.yaml"

    @property
    def template_ytdlp_config_path(self) -> Path:
        return self.project_root / "config" / "yt-dlp.conf"

    @property
    def runtime_beets_config_path(self) -> Path:
        return self.runtime_config_dir / "beets.yaml"

    @property
    def runtime_ytdlp_config_path(self) -> Path:
        return self.runtime_config_dir / "yt-dlp.conf"

    @property
    def cookies_available(self) -> bool:
        return self.ytdlp_cookies_file.is_file() and os.access(self.ytdlp_cookies_file, os.R_OK)

    def prepare_runtime(self) -> None:
        self.music_library_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.app_state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_config_dir.mkdir(parents=True, exist_ok=True)
        self.playlist_dir.mkdir(parents=True, exist_ok=True)
        self.beets_library_path.parent.mkdir(parents=True, exist_ok=True)
        self._render_template(self.template_beets_config_path, self.runtime_beets_config_path)
        self._render_template(self.template_ytdlp_config_path, self.runtime_ytdlp_config_path)

    def _render_template(self, source: Path, target: Path) -> None:
        rendered = source.read_text(encoding="utf-8")
        rendered = rendered.replace("{{MUSIC_LIBRARY_DIR}}", self.music_library_dir.as_posix())
        rendered = rendered.replace("{{APP_STATE_DIR}}", self.app_state_dir.as_posix())
        rendered = rendered.replace("{{STAGING_DIR}}", self.staging_dir.as_posix())
        target.write_text(rendered, encoding="utf-8")
