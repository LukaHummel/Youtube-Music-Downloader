from __future__ import annotations

import logging

from telegram import Update

from .beets_runner import BeetsRunner
from .bot import TelegramBotService
from .config import AppConfig
from .db import Database
from .playlist_writer import PlaylistWriter
from .worker import JobWorker
from .ytdlp_runner import YtDlpRunner

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
    logging.getLogger(__name__).info(
        "Starting YouTube Music downloader bot with app log level %s and external log level %s",
        config.log_level,
        config.external_log_level,
    )
    logging.getLogger(__name__).info(
        "Runtime paths: music=%s staging=%s state=%s cookies=%s",
        config.music_library_dir,
        config.staging_dir,
        config.app_state_dir,
        "available" if config.cookies_available else "not available",
    )
    if not config.cookies_available:
        logging.getLogger(__name__).warning(
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


if __name__ == "__main__":
    main()
