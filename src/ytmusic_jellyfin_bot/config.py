from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_TEMPLATE_DIR_ENV = "CONFIG_TEMPLATE_DIR"
CONFIG_TEMPLATE_FILENAMES = ("beets.yaml", "yt-dlp.conf")


def _parse_allowed_ids(raw_value: str) -> frozenset[int]:
    ids: set[int] = set()
    for piece in raw_value.split(","):
        value = piece.strip()
        if value:
            ids.add(int(value))
    return frozenset(ids)


def _getenv_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    value = float(raw_value)
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _getenv_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    return int(raw_value)


def _has_config_templates(directory: Path) -> bool:
    return all((directory / filename).is_file() for filename in CONFIG_TEMPLATE_FILENAMES)


def _resolve_config_template_dir(project_root: Path, cwd: Path | None = None) -> Path:
    configured = os.environ.get(CONFIG_TEMPLATE_DIR_ENV, "").strip()
    if configured:
        directory = Path(configured).expanduser().resolve()
        if _has_config_templates(directory):
            return directory
        expected = ", ".join(CONFIG_TEMPLATE_FILENAMES)
        raise FileNotFoundError(
            f"{CONFIG_TEMPLATE_DIR_ENV} must point to a directory containing {expected}: {directory}"
        )

    current_dir = cwd or Path.cwd()
    package_dir = Path(__file__).resolve().parent
    candidates = (
        project_root / "config",
        current_dir / "config",
        Path("/app/config"),
        package_dir / "config",
    )
    for directory in candidates:
        if _has_config_templates(directory):
            return directory.resolve()

    expected = ", ".join(CONFIG_TEMPLATE_FILENAMES)
    searched = ", ".join(str(directory.resolve()) for directory in candidates)
    raise FileNotFoundError(
        f"Could not find config templates ({expected}). "
        f"Set {CONFIG_TEMPLATE_DIR_ENV} to the template directory. Searched: {searched}"
    )


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
    external_log_level: str
    project_root: Path
    config_template_dir: Path | None = None
    telegram_connect_timeout: float = 30.0
    telegram_read_timeout: float = 30.0
    telegram_write_timeout: float = 30.0
    telegram_pool_timeout: float = 30.0
    telegram_poll_timeout: int = 10
    telegram_bootstrap_retries: int = -1

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
            external_log_level=os.environ.get("EXTERNAL_LOG_LEVEL", "WARNING").upper(),
            project_root=project_root,
            config_template_dir=_resolve_config_template_dir(project_root),
            telegram_connect_timeout=_getenv_float("TELEGRAM_CONNECT_TIMEOUT", 30.0),
            telegram_read_timeout=_getenv_float("TELEGRAM_READ_TIMEOUT", 30.0),
            telegram_write_timeout=_getenv_float("TELEGRAM_WRITE_TIMEOUT", 30.0),
            telegram_pool_timeout=_getenv_float("TELEGRAM_POOL_TIMEOUT", 30.0),
            telegram_poll_timeout=_getenv_int("TELEGRAM_POLL_TIMEOUT", 10),
            telegram_bootstrap_retries=_getenv_int("TELEGRAM_BOOTSTRAP_RETRIES", -1),
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
        return self.template_config_dir / "beets.yaml"

    @property
    def template_ytdlp_config_path(self) -> Path:
        return self.template_config_dir / "yt-dlp.conf"

    @property
    def template_config_dir(self) -> Path:
        return self.config_template_dir or _resolve_config_template_dir(self.project_root)

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
