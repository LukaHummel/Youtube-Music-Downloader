from __future__ import annotations

import logging
import platform
import sys
from importlib.metadata import PackageNotFoundError, version as package_version

from telegram import Update

from . import __version__
from .beets_runner import BeetsRunner
from .bot import TelegramBotService
from .config import AppConfig
from .db import Database
from .playlist_writer import PlaylistWriter
from .worker import JobWorker
from .ytdlp_runner import DEFAULT_FORMAT_SELECTOR, YtDlpRunner

DEPENDENCY_VERSION_PACKAGES = ("yt-dlp", "beets", "python-telegram-bot")
EXTERNAL_LOGGERS = ("telegram", "httpx", "httpcore")
PLAIN_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
COLOR_LOG_FORMAT = "%(asctime)s %(colored_level)s %(name)s: %(message)s"
RESET = "\033[0m"
LEVEL_COLORS = {
    logging.DEBUG: "\033[35m",
    logging.INFO: "\033[36m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}


class ColorLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = LEVEL_COLORS.get(record.levelno, "")
        record.colored_level = f"{color}[{record.levelname}]{RESET}" if color else f"[{record.levelname}]"
        try:
            return super().format(record)
        finally:
            del record.colored_level


def main() -> None:
    config = AppConfig.from_env()
    config.prepare_runtime()
    _configure_logging(config)
    logger = logging.getLogger(__name__)
    logger.info(
        "Starting YouTube Music downloader bot version=%s python=%s platform=%s "
        "app_log_level=%s external_log_level=%s color_logs=%s",
        __version__,
        _python_version(),
        platform.platform(),
        config.log_level,
        config.external_log_level,
        config.color_logs,
    )
    logger.info(
        "Runtime configuration: worker_concurrency=%s telegram_poll_timeout=%ss "
        "telegram_bootstrap_retries=%s db=%s config_templates=%s",
        config.worker_concurrency,
        config.telegram_poll_timeout,
        config.telegram_bootstrap_retries,
        config.db_path,
        config.template_config_dir,
    )
    logger.info("Dependency versions: %s", _dependency_versions())
    logger.info(
        "Runtime paths: music=%s staging=%s state=%s cookies=%s",
        config.music_library_dir,
        config.staging_dir,
        config.app_state_dir,
        "available" if config.cookies_available else "not available",
    )
    logger.info(
        "yt-dlp runtime config=%s config_format=%s enforced_format=%s youtube_clients=default preflight_config=ignored preflight_formats=ignored",
        config.runtime_ytdlp_config_path,
        _find_config_format(config.runtime_ytdlp_config_path),
        DEFAULT_FORMAT_SELECTOR,
    )
    if not config.cookies_available:
        logger.warning(
            "Cookies file is not mounted or readable at %s; private, age-restricted, and bot-check gated "
            "YouTube requests may fail",
            config.ytdlp_cookies_file,
        )
    db = Database(config.db_path)
    worker = JobWorker(
        config=config,
        db=db,
        ytdlp=YtDlpRunner(config),
        beets=BeetsRunner(config),
        playlist_writer=PlaylistWriter(config),
    )
    bot = TelegramBotService(config=config, db=db, worker=worker)
    application = bot.build()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        timeout=config.telegram_poll_timeout,
        bootstrap_retries=config.telegram_bootstrap_retries,
    )


def _configure_logging(config: AppConfig) -> None:
    app_level = _parse_log_level(config.log_level, logging.INFO)
    external_level = _parse_log_level(config.external_log_level, logging.WARNING)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.WARNING)

    handler = logging.StreamHandler()
    if config.color_logs:
        handler.setFormatter(ColorLogFormatter(COLOR_LOG_FORMAT))
    else:
        handler.setFormatter(logging.Formatter(PLAIN_LOG_FORMAT))
    root_logger.addHandler(handler)

    logging.getLogger("ytmusic_jellyfin_bot").setLevel(app_level)
    for logger_name in EXTERNAL_LOGGERS:
        logging.getLogger(logger_name).setLevel(external_level)


def _parse_log_level(value: str, default: int) -> int:
    level = getattr(logging, value.upper(), None)
    return level if isinstance(level, int) else default


def _python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def _dependency_versions() -> str:
    versions: list[str] = []
    for package_name in DEPENDENCY_VERSION_PACKAGES:
        try:
            installed_version = package_version(package_name)
        except PackageNotFoundError:
            installed_version = "not installed"
        versions.append(f"{package_name}={installed_version}")
    return " ".join(versions)


def _find_config_format(config_path) -> str:
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unreadable"
    for index, line in enumerate(lines):
        if line.strip() in {"-f", "--format"} and index + 1 < len(lines):
            return lines[index + 1].strip()
    return "not set"


if __name__ == "__main__":
    main()
