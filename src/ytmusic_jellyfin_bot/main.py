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
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


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
    logging.basicConfig(
        level=logging.WARNING,
        format=LOG_FORMAT,
    )
    logging.getLogger("ytmusic_jellyfin_bot").setLevel(app_level)
    for logger_name in EXTERNAL_LOGGERS:
        logging.getLogger(logger_name).setLevel(external_level)


def _parse_log_level(value: str, default: int) -> int:
    level = getattr(logging, value.upper(), None)
    return level if isinstance(level, int) else default


if __name__ == "__main__":
    main()
