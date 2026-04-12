from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import AppConfig
from .db import Database
from .models import JobStatus, RequestKind
from .normalizer import NormalizationError, normalize_url
from .worker import JobWorker

LOGGER = logging.getLogger(__name__)


class TelegramBotService:
    def __init__(self, *, config: AppConfig, db: Database, worker: JobWorker):
        self.config = config
        self.db = db
        self.worker = worker
        self.application: Application | None = None

    def build(self) -> Application:
        application = (
            Application.builder()
            .token(self.config.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("track", self.track_command))
        application.add_handler(CommandHandler("playlist", self.playlist_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("jobs", self.jobs_command))
        application.add_handler(CommandHandler("retry", self.retry_command))
        application.add_handler(CommandHandler("cancel", self.cancel_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))
        self.application = application
        return application

    async def _post_init(self, application: Application) -> None:
        self.worker.set_notifier(self.send_notification)
        await self.worker.start()

    async def _post_shutdown(self, application: Application) -> None:
        await self.worker.stop()

    async def send_notification(self, chat_id: int, message: str, job_id: int | None) -> None:
        assert self.application is not None
        await self.application.bot.send_message(chat_id=chat_id, text=message)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        text = (
            "Send a YouTube or YouTube Music track or playlist URL to queue it.\n\n"
            "/track <url> forces single-track handling.\n"
            "/playlist <url> forces playlist handling.\n"
            "/status shows the active job and queue depth.\n"
            "/status <job_id> shows one job.\n"
            "/jobs lists recent jobs.\n"
            "/retry <job_id> requeues a failed, partial, or cancelled job.\n"
            "/cancel <job_id> cancels a queued job or requests cancellation for the active one.\n\n"
            "Private playlists require a valid mounted cookies.txt file."
        )
        if update.effective_message:
            await update.effective_message.reply_text(text)

    async def track_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._submit_from_command(update, context, RequestKind.TRACK)

    async def playlist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._submit_from_command(update, context, RequestKind.PLAYLIST)

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        text = (update.effective_message.text or "").strip()
        if "youtube.com" not in text and "youtu.be" not in text and "music.youtube.com" not in text:
            await update.effective_message.reply_text("Send a YouTube or YouTube Music URL.")
            return
        await self._submit_job(update, text, None)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if context.args:
            try:
                job = self.db.get_job(int(context.args[0]))
            except (ValueError, KeyError):
                await update.effective_message.reply_text("Unknown job ID.")
                return
            items = self.db.get_job_items(job.id)
            current = f"{job.current_item_index}/{job.total_items}" if job.total_items else "0/0"
            percent = f"{job.progress_percent:.1f}%" if job.progress_percent is not None else "n/a"
            eta = f"{job.progress_eta_seconds}s" if job.progress_eta_seconds is not None else "n/a"
            text = (
                f"Job #{job.id}\n"
                f"Status: {job.status}\n"
                f"Type: {job.request_kind}\n"
                f"Items: {current}\n"
                f"Progress: {percent}\n"
                f"ETA: {eta}\n"
                f"Speed: {job.progress_speed or 'n/a'}\n"
                f"Summary: {job.result_summary or 'n/a'}\n"
                f"Error: {job.error_message or 'n/a'}\n"
                f"Tracked items: {len(items)}"
            )
            await update.effective_message.reply_text(text)
            return

        active = self.db.get_active_job()
        queue_depth = self.db.count_queued_jobs()
        if not active:
            await update.effective_message.reply_text(f"No active job. Queue depth: {queue_depth}")
            return
        lines = [
            f"Active job: #{active.id}",
            f"Status: {active.status}",
            f"Item: {active.current_item_index}/{active.total_items}",
            f"Queue depth: {queue_depth}",
        ]
        if active.progress_percent is not None:
            lines.insert(3, f"Progress: {active.progress_percent:.1f}%")
        await update.effective_message.reply_text("\n".join(lines))

    async def jobs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        jobs = self.db.list_recent_jobs()
        if not jobs:
            await update.effective_message.reply_text("No jobs recorded yet.")
            return
        lines = [
            f"#{job.id} {job.status} {job.request_kind} {job.current_item_index}/{job.total_items} {job.source_title or job.source_url}"
            for job in jobs
        ]
        await update.effective_message.reply_text("\n".join(lines))

    async def retry_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if not context.args:
            await update.effective_message.reply_text("Usage: /retry <job_id>")
            return
        try:
            job_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Job ID must be numeric.")
            return
        if not self.db.requeue_job(job_id):
            await update.effective_message.reply_text("Only failed, partial, or cancelled jobs can be retried.")
            return
        self.worker.wake()
        await update.effective_message.reply_text(f"Job #{job_id} requeued.")

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if not context.args:
            await update.effective_message.reply_text("Usage: /cancel <job_id>")
            return
        try:
            job_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Job ID must be numeric.")
            return
        if self.db.cancel_if_queued(job_id):
            await update.effective_message.reply_text(f"Queued job #{job_id} cancelled.")
            return
        try:
            job = self.db.get_job(job_id)
        except KeyError:
            await update.effective_message.reply_text("Unknown job ID.")
            return
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            await update.effective_message.reply_text(f"Job #{job_id} is already finished.")
            return
        self.db.mark_cancel_requested(job_id)
        await update.effective_message.reply_text(f"Cancellation requested for job #{job_id}.")

    async def _submit_from_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        request_kind: RequestKind,
    ) -> None:
        if not await self._ensure_allowed(update):
            return
        if not context.args:
            await update.effective_message.reply_text(f"Usage: /{request_kind.value} <url>")
            return
        await self._submit_job(update, context.args[0], request_kind)

    async def _submit_job(
        self, update: Update, raw_url: str, forced_kind: RequestKind | None
    ) -> None:
        try:
            normalized = normalize_url(raw_url, forced_kind)
        except NormalizationError as exc:
            await update.effective_message.reply_text(str(exc))
            return

        chat_id = update.effective_chat.id
        user = update.effective_user
        requested_by = user.username or user.full_name if user else None
        job = self.db.create_job(
            source_url=normalized.source_url,
            normalized_url=normalized.normalized_url,
            request_kind=normalized.request_kind,
            chat_id=chat_id,
            user_id=user.id if user else 0,
            requested_by=requested_by,
            source_id=normalized.youtube_video_id or normalized.playlist_id,
        )
        self.db.add_message(
            direction="incoming",
            body=update.effective_message.text or raw_url,
            job_id=job.id,
            telegram_chat_id=chat_id,
            telegram_user_id=user.id if user else None,
        )
        self.worker.wake()
        await update.effective_message.reply_text(
            f"Queued job #{job.id} as {normalized.request_kind}.\n{normalized.normalized_url}"
        )

    async def _ensure_allowed(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        identifiers = {
            identifier
            for identifier in (user.id if user else None, chat.id if chat else None)
            if identifier is not None
        }
        if identifiers & self.config.allowed_telegram_ids:
            return True
        LOGGER.warning(
            "Rejected Telegram access from user=%s chat=%s",
            user.id if user else None,
            chat.id if chat else None,
        )
        if update.effective_message:
            await update.effective_message.reply_text("This bot is restricted to configured Telegram IDs.")
        return False
