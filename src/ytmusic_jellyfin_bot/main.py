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


def main() -> None:
    config = AppConfig.from_env()
    config.prepare_runtime()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
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
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
