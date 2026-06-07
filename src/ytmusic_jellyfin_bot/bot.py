from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import escape
from time import monotonic

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .config import AppConfig
from .db import Database
from .models import ItemStatus, JobItemRecord, JobRecord, JobStatus, RequestKind
from .normalizer import NormalizationError, normalize_url
from .worker import JobWorker
from .ytmusic_auth import YtMusicAuthManager, YtMusicAuthStartStatus
from .ytmusic_metadata import YtMusicMetadataProvider

LOGGER = logging.getLogger(__name__)
YOUTUBE_URL_RE = re.compile(
    r"(?P<url>"
    r"(?:https?://)?"
    r"(?:(?:www\.|m\.)?youtube\.com|(?:www\.)?music\.youtube\.com|youtu\.be)"
    r"/[^\s<>()\[\]{}]+"
    r")",
    re.IGNORECASE,
)
TRAILING_URL_PUNCTUATION = ".,;:!?\"'"
JOB_CALLBACK_RE = re.compile(r"^job:(?P<action>cancel|retry):(?P<job_id>\d+)$")
PROGRESS_THROTTLE_SECONDS = 2.0
PROGRESS_THROTTLE_PERCENT_DELTA = 2.0
PROGRESS_BAR_WIDTH = 10
SOURCE_LABEL_MAX_LENGTH = 160
CURRENT_ITEM_MAX_LENGTH = 180
RESULT_SUMMARY_MAX_LENGTH = 600
ERROR_MESSAGE_MAX_LENGTH = 900
ACTIVE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.NORMALIZING,
    JobStatus.PREFLIGHT,
    JobStatus.DOWNLOADING,
    JobStatus.RETAGGING,
    JobStatus.PLAYLIST_BUILDING,
}
RETRYABLE_JOB_STATUSES = {JobStatus.FAILED, JobStatus.PARTIAL, JobStatus.CANCELLED}


@dataclass(slots=True)
class _ProgressEditState:
    rendered_text: str | None = None
    last_edit_monotonic: float = 0.0
    last_percent: float | None = None


class TelegramBotService:
    def __init__(
        self,
        *,
        config: AppConfig,
        db: Database,
        worker: JobWorker,
        ytmusic_auth: YtMusicAuthManager | None = None,
        ytmusic_metadata: YtMusicMetadataProvider | None = None,
    ):
        self.config = config
        self.db = db
        self.worker = worker
        self.ytmusic_auth = ytmusic_auth
        self.ytmusic_metadata = ytmusic_metadata
        self.application: Application | None = None
        self._progress_edit_state: dict[int, _ProgressEditState] = {}

    def build(self) -> Application:
        application = (
            Application.builder()
            .token(self.config.telegram_bot_token)
            .connect_timeout(self.config.telegram_connect_timeout)
            .read_timeout(self.config.telegram_read_timeout)
            .write_timeout(self.config.telegram_write_timeout)
            .pool_timeout(self.config.telegram_pool_timeout)
            .get_updates_connect_timeout(self.config.telegram_connect_timeout)
            .get_updates_read_timeout(self.config.telegram_read_timeout)
            .get_updates_write_timeout(self.config.telegram_write_timeout)
            .get_updates_pool_timeout(self.config.telegram_pool_timeout)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        application.add_handler(CommandHandler("start", self.help_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("track", self.track_command))
        application.add_handler(CommandHandler("playlist", self.playlist_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("jobs", self.jobs_command))
        application.add_handler(CommandHandler("retry", self.retry_command))
        application.add_handler(CommandHandler("cancel", self.cancel_command))
        application.add_handler(CommandHandler("ytmusic_auth", self.ytmusic_auth_command))
        application.add_handler(CommandHandler("ytmusic_auth_status", self.ytmusic_auth_status_command))
        application.add_handler(CommandHandler("ytmusic_auth_reset", self.ytmusic_auth_reset_command))
        application.add_handler(CallbackQueryHandler(self.job_callback, pattern=JOB_CALLBACK_RE.pattern))
        plain_url_filter = (filters.TEXT | filters.CAPTION) & ~filters.COMMAND
        application.add_handler(MessageHandler(plain_url_filter, self.text_message))
        self.application = application
        return application

    async def _post_init(self, application: Application) -> None:
        self.worker.set_notifier(self.send_notification)
        self.worker.set_progress_notifier(self.update_progress_card)
        await self.worker.start()

    async def _post_shutdown(self, application: Application) -> None:
        await self.worker.stop()

    async def send_notification(self, chat_id: int, message: str, job_id: int | None) -> None:
        assert self.application is not None
        LOGGER.debug("Sending Telegram message for job_id=%s chat_id=%s", job_id, chat_id)
        await self.application.bot.send_message(chat_id=chat_id, text=message)

    async def update_progress_card(self, job_id: int, force: bool) -> None:
        try:
            job = self.db.get_job(job_id)
        except KeyError:
            LOGGER.debug("Skipping progress card update for unknown job_id=%s", job_id)
            return

        if not force and not self._should_edit_progress(job):
            return

        text, reply_markup = self._render_progress_payload(job)
        state = self._progress_edit_state.get(job.id)
        if state and state.rendered_text == text:
            return

        if job.progress_message_id is None:
            await self._send_replacement_progress_card(job, text, reply_markup)
            return

        if self.application is None:
            LOGGER.debug("Skipping progress card edit before Telegram application is available")
            return

        try:
            await self.application.bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.progress_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            lowered = str(exc).lower()
            if "message is not modified" in lowered:
                self._remember_progress_state(job, text)
                return
            if _is_uneditable_message_error(lowered):
                await self._send_replacement_progress_card(job, text, reply_markup)
                return
            LOGGER.warning("Telegram progress card edit failed for job_id=%s: %s", job.id, exc)
            return
        except RetryAfter as exc:
            LOGGER.warning(
                "Telegram rate-limited progress card edit for job_id=%s retry_after=%ss",
                job.id,
                exc.retry_after,
            )
            return
        except (NetworkError, TimedOut) as exc:
            LOGGER.warning("Telegram progress card edit skipped for job_id=%s: %s", job.id, exc)
            return

        self._remember_progress_state(job, text)

    async def job_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not await self._ensure_allowed(update):
            await self._answer_callback(query, "This bot is restricted.", show_alert=True)
            return

        match = JOB_CALLBACK_RE.match(query.data or "")
        if match is None:
            await self._answer_callback(query, "Unknown action.")
            return

        action = match.group("action")
        job_id = int(match.group("job_id"))
        if action == "cancel":
            await self._cancel_from_callback(query, job_id)
            return
        if action == "retry":
            await self._retry_from_callback(query, job_id)
            return

        await self._answer_callback(query, "Unknown action.")

    async def _cancel_from_callback(self, query, job_id: int) -> None:
        if self.db.cancel_if_queued(job_id):
            await self.update_progress_card(job_id, True)
            await self._answer_callback(query, f"Job #{job_id} cancelled.")
            return
        try:
            job = self.db.get_job(job_id)
        except KeyError:
            await self._answer_callback(query, "Unknown job.")
            return
        if job.status not in ACTIVE_JOB_STATUSES:
            await self.update_progress_card(job_id, True)
            await self._answer_callback(query, f"Job #{job_id} is already finished.")
            return
        self.db.mark_cancel_requested(job_id)
        await self.update_progress_card(job_id, True)
        await self._answer_callback(query, f"Cancellation requested for job #{job_id}.")

    async def _retry_from_callback(self, query, job_id: int) -> None:
        if not self.db.requeue_job(job_id):
            await self.update_progress_card(job_id, True)
            await self._answer_callback(query, "Only failed, partial, or cancelled jobs can be retried.")
            return
        self.worker.wake()
        await self.update_progress_card(job_id, True)
        await self._answer_callback(query, f"Job #{job_id} requeued.")

    async def _answer_callback(self, query, text: str, *, show_alert: bool = False) -> None:
        try:
            await query.answer(text=text, show_alert=show_alert)
        except (BadRequest, NetworkError, RetryAfter, TimedOut) as exc:
            LOGGER.warning("Telegram callback answer failed: %s", exc)

    async def _send_initial_progress_card(self, reply_to: Message, job: JobRecord) -> None:
        text, reply_markup = self._render_progress_payload(job)
        try:
            sent = await reply_to.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except (BadRequest, NetworkError, RetryAfter, TimedOut) as exc:
            LOGGER.warning("Telegram initial progress card send failed for job_id=%s: %s", job.id, exc)
            await self._send_replacement_progress_card(job, text, reply_markup)
            return

        message_id = getattr(sent, "message_id", None)
        if message_id is not None:
            self.db.update_job(job.id, progress_message_id=int(message_id))
        self._remember_progress_state(job, text)

    async def _send_replacement_progress_card(
        self,
        job: JobRecord,
        text: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> None:
        if self.application is None:
            LOGGER.debug("Skipping replacement progress card before Telegram application is available")
            return
        try:
            sent = await self.application.bot.send_message(
                chat_id=job.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except RetryAfter as exc:
            LOGGER.warning(
                "Telegram rate-limited replacement progress card for job_id=%s retry_after=%ss",
                job.id,
                exc.retry_after,
            )
            return
        except (BadRequest, NetworkError, TimedOut) as exc:
            LOGGER.warning("Telegram replacement progress card failed for job_id=%s: %s", job.id, exc)
            return

        message_id = getattr(sent, "message_id", None)
        if message_id is not None:
            self.db.update_job(job.id, progress_message_id=int(message_id))
        self._remember_progress_state(job, text)

    def _render_progress_payload(self, job: JobRecord) -> tuple[str, InlineKeyboardMarkup]:
        items = self.db.get_job_items(job.id)
        return _render_progress_card(job, items), _build_progress_keyboard(job)

    def _should_edit_progress(self, job: JobRecord) -> bool:
        state = self._progress_edit_state.get(job.id)
        if state is None:
            return True
        now = monotonic()
        elapsed = now - state.last_edit_monotonic
        if elapsed >= PROGRESS_THROTTLE_SECONDS:
            return True
        percent_changed = False
        if job.progress_percent != state.last_percent:
            if job.progress_percent is None or state.last_percent is None:
                percent_changed = True
            else:
                percent_changed = abs(job.progress_percent - state.last_percent) >= PROGRESS_THROTTLE_PERCENT_DELTA
        return percent_changed

    def _remember_progress_state(self, job: JobRecord, rendered_text: str) -> None:
        self._progress_edit_state[job.id] = _ProgressEditState(
            rendered_text=rendered_text,
            last_edit_monotonic=monotonic(),
            last_percent=job.progress_percent,
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        text = (
            "Send a YouTube or YouTube Music track or playlist URL to queue it.\n\n"
            "/track <url> forces single-track handling.\n"
            "/playlist <url> forces playlist handling.\n"
            "/status shows the active job and queue depth.\n"
            "/status <job_id> shows one job.\n"
            "/jobs lists recent jobs.\n"
            "/retry <job_id> requeues a failed, partial, or cancelled job.\n"
            "/cancel <job_id> cancels a queued job or requests cancellation for the active one.\n"
            "/ytmusic_auth starts YouTube Music OAuth metadata setup.\n"
            "/ytmusic_auth_status shows YouTube Music OAuth metadata status.\n"
            "/ytmusic_auth_reset removes the saved YouTube Music OAuth token.\n\n"
            "Private playlists require a valid mounted cookies.txt file."
        )
        await message.reply_text(text)

    async def track_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._submit_from_command(update, context, RequestKind.TRACK)

    async def playlist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._submit_from_command(update, context, RequestKind.PLAYLIST)

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        text = (message.text or message.caption or "").strip()
        url = _extract_supported_url(text)
        if url is None:
            await message.reply_text("Send a YouTube or YouTube Music URL.")
            return
        await self._submit_job(update, url, None)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        if context.args:
            try:
                job = self.db.get_job(int(context.args[0]))
            except (ValueError, KeyError):
                await message.reply_text("Unknown job ID.")
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
            await message.reply_text(text)
            return

        active = self.db.get_active_job()
        queue_depth = self.db.count_queued_jobs()
        if not active:
            await message.reply_text(f"No active job. Queue depth: {queue_depth}")
            return
        lines = [
            f"Active job: #{active.id}",
            f"Status: {active.status}",
            f"Item: {active.current_item_index}/{active.total_items}",
            f"Queue depth: {queue_depth}",
        ]
        if active.progress_percent is not None:
            lines.insert(3, f"Progress: {active.progress_percent:.1f}%")
        await message.reply_text("\n".join(lines))

    async def jobs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        jobs = self.db.list_recent_jobs()
        if not jobs:
            await message.reply_text("No jobs recorded yet.")
            return
        lines = [
            f"#{job.id} {job.status} {job.request_kind} {job.current_item_index}/{job.total_items} {job.source_title or job.source_url}"
            for job in jobs
        ]
        await message.reply_text("\n".join(lines))

    async def retry_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        if not context.args:
            await message.reply_text("Usage: /retry <job_id>")
            return
        try:
            job_id = int(context.args[0])
        except ValueError:
            await message.reply_text("Job ID must be numeric.")
            return
        if not self.db.requeue_job(job_id):
            await message.reply_text("Only failed, partial, or cancelled jobs can be retried.")
            return
        self.worker.wake()
        await self.update_progress_card(job_id, True)
        await message.reply_text(f"Job #{job_id} requeued.")

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        if not context.args:
            await message.reply_text("Usage: /cancel <job_id>")
            return
        try:
            job_id = int(context.args[0])
        except ValueError:
            await message.reply_text("Job ID must be numeric.")
            return
        if self.db.cancel_if_queued(job_id):
            await self.update_progress_card(job_id, True)
            await message.reply_text(f"Queued job #{job_id} cancelled.")
            return
        try:
            job = self.db.get_job(job_id)
        except KeyError:
            await message.reply_text("Unknown job ID.")
            return
        if job.status in {JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED}:
            await message.reply_text(f"Job #{job_id} is already finished.")
            return
        self.db.mark_cancel_requested(job_id)
        await self.update_progress_card(job_id, True)
        await message.reply_text(f"Cancellation requested for job #{job_id}.")

    async def ytmusic_auth_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        chat = self._require_chat(update)
        if not self.ytmusic_auth:
            await message.reply_text("YouTube Music metadata authentication is not configured.")
            return

        result = await self.ytmusic_auth.start_auth_flow(
            lambda body: self._complete_ytmusic_auth(chat.id, body)
        )
        if result.status is YtMusicAuthStartStatus.DISABLED:
            await message.reply_text("YouTube Music metadata enrichment is disabled.")
            return
        if result.status is YtMusicAuthStartStatus.MISSING_CLIENT_CREDENTIALS:
            await message.reply_text(
                "YouTube Music OAuth is missing client credentials. Set "
                "YTMUSIC_OAUTH_CLIENT_ID and YTMUSIC_OAUTH_CLIENT_SECRET."
            )
            return
        assert result.flow is not None
        prefix = (
            "YouTube Music OAuth is already in progress."
            if result.status is YtMusicAuthStartStatus.ALREADY_IN_PROGRESS
            else "Started YouTube Music OAuth setup."
        )
        await message.reply_text(
            f"{prefix}\n"
            f"Open: {result.flow.verification_url_with_code}\n"
            f"Code: {result.flow.user_code}\n"
            f"Expires in: {result.flow.remaining_seconds}s"
        )

    async def ytmusic_auth_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        if not self.ytmusic_auth:
            await message.reply_text("YouTube Music OAuth status: disabled")
            return
        await message.reply_text(f"YouTube Music OAuth status: {self.ytmusic_auth.status()}")

    async def ytmusic_auth_reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        if not self.ytmusic_auth:
            await message.reply_text("YouTube Music metadata authentication is not configured.")
            return
        await self.ytmusic_auth.reset()
        if self.ytmusic_metadata:
            self.ytmusic_metadata.clear_client_cache()
        await message.reply_text("YouTube Music OAuth token reset.")

    async def _complete_ytmusic_auth(self, chat_id: int, body: str) -> None:
        if self.ytmusic_metadata:
            self.ytmusic_metadata.clear_client_cache()
        await self.send_notification(chat_id, body, None)

    async def _submit_from_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        request_kind: RequestKind,
    ) -> None:
        if not await self._ensure_allowed(update):
            return
        message = self._require_message(update)
        if not context.args:
            await message.reply_text(f"Usage: /{request_kind.value} <url>")
            return
        await self._submit_job(update, context.args[0], request_kind)

    async def _submit_job(
        self, update: Update, raw_url: str, forced_kind: RequestKind | None
    ) -> None:
        message = self._require_message(update)
        chat = self._require_chat(update)
        try:
            normalized = normalize_url(raw_url, forced_kind)
        except NormalizationError as exc:
            await message.reply_text(str(exc))
            return

        chat_id = chat.id
        user = update.effective_user
        requested_by = (user.username or user.full_name) if user else None
        requester = (requested_by or str(user.id)) if user else "unknown"
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
            body=message.text or message.caption or raw_url,
            job_id=job.id,
            telegram_chat_id=chat_id,
            telegram_user_id=user.id if user else None,
        )
        LOGGER.info(
            "Queued job #%s from Telegram: type=%s user=%s chat_id=%s url=%s",
            job.id,
            normalized.request_kind,
            requester,
            chat_id,
            normalized.normalized_url,
        )
        await self._send_initial_progress_card(message, job)
        self.worker.wake()

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
        message = update.effective_message
        if message is not None:
            await message.reply_text("This bot is restricted to configured Telegram IDs.")
        return False

    @staticmethod
    def _require_message(update: Update) -> Message:
        message = update.effective_message
        if message is None:
            raise ValueError("Telegram update does not include a message.")
        return message

    @staticmethod
    def _require_chat(update: Update) -> Chat:
        chat = update.effective_chat
        if chat is None:
            raise ValueError("Telegram update does not include a chat.")
        return chat


def _render_progress_card(job: JobRecord, items: list[JobItemRecord]) -> str:
    source_label = job.source_title or job.playlist_title or job.normalized_url
    lines = [
        f"<b>Job #{job.id}</b>",
        f"Status: <b>{_html(_pretty_status(job.status))}</b>",
        f"Type: {_html(_pretty_status(job.request_kind))}",
        f"Source: {_html(_truncate(source_label, SOURCE_LABEL_MAX_LENGTH))}",
        f"Items: {job.current_item_index}/{job.total_items}",
    ]

    current_item = _current_item_for(job, items)
    if current_item:
        current_label = _format_current_item(current_item)
        if current_label:
            lines.append(f"Current: {_html(_truncate(current_label, CURRENT_ITEM_MAX_LENGTH))}")

    lines.append(f"Progress: {_html(_format_progress(job.progress_percent))}")

    if job.progress_eta_seconds is not None:
        lines.append(f"ETA: {_html(_format_eta(job.progress_eta_seconds))}")
    if job.progress_speed:
        lines.append(f"Speed: {_html(_truncate(job.progress_speed, 80))}")
    if job.cancel_requested:
        lines.append("Cancellation: requested")
    if job.result_summary:
        lines.extend(("", f"Result: {_html(_truncate(job.result_summary, RESULT_SUMMARY_MAX_LENGTH))}"))
    if job.error_message:
        lines.extend(("", f"Error: {_html(_truncate(job.error_message, ERROR_MESSAGE_MAX_LENGTH))}"))

    return "\n".join(lines)


def _build_progress_keyboard(job: JobRecord) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if job.status in ACTIVE_JOB_STATUSES and not job.cancel_requested:
        rows.append([InlineKeyboardButton("Cancel", callback_data=f"job:cancel:{job.id}")])

    if job.status in RETRYABLE_JOB_STATUSES:
        rows.append([InlineKeyboardButton("Retry", callback_data=f"job:retry:{job.id}")])
    if job.normalized_url:
        rows.append([InlineKeyboardButton("Open source", url=job.normalized_url)])
    return InlineKeyboardMarkup(rows)


def _current_item_for(job: JobRecord, items: list[JobItemRecord]) -> JobItemRecord | None:
    if not items:
        return None
    if job.current_item_index:
        for item in items:
            if item.item_index == job.current_item_index:
                return item
    for item in items:
        if item.status in {ItemStatus.DOWNLOADING, ItemStatus.RETAGGING}:
            return item
    return None


def _format_current_item(item: JobItemRecord) -> str | None:
    if item.artist and item.title:
        return f"{item.artist} - {item.title}"
    return item.title or item.artist or None


def _format_progress(percent: float | None) -> str:
    if percent is None:
        return "n/a"
    clamped = max(0.0, min(100.0, percent))
    filled = round((clamped / 100.0) * PROGRESS_BAR_WIDTH)
    bar = "#" * filled + "." * (PROGRESS_BAR_WIDTH - filled)
    return f"[{bar}] {clamped:.1f}%"


def _format_eta(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def _pretty_status(value: JobStatus | RequestKind) -> str:
    return str(value).replace("_", " ").title()


def _html(value: str) -> str:
    return escape(value, quote=False)


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3]}..."


def _is_uneditable_message_error(lowered_error: str) -> bool:
    markers = (
        "message to edit not found",
        "message can't be edited",
        "message identifier is not specified",
        "message_id_invalid",
    )
    return any(marker in lowered_error for marker in markers)


def _extract_supported_url(text: str) -> str | None:
    match = YOUTUBE_URL_RE.search(text)
    if match is None:
        return None
    url = match.group("url").rstrip(TRAILING_URL_PUNCTUATION)
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    return url
