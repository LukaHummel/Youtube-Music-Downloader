import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telegram.error import BadRequest

from ytmusic_jellyfin_bot.bot import (
    TelegramBotService,
    _build_progress_keyboard,
    _extract_supported_url,
    _render_progress_card,
)
from ytmusic_jellyfin_bot.db import Database
from ytmusic_jellyfin_bot.models import ItemStatus, JobStatus, PreflightItem, RequestKind
from ytmusic_jellyfin_bot.ytmusic_auth import (
    YtMusicAuthFlow,
    YtMusicAuthStartResult,
    YtMusicAuthStartStatus,
    YtMusicAuthStatus,
)


class BotTests(unittest.TestCase):
    def test_extracts_bare_music_url(self) -> None:
        self.assertEqual(
            _extract_supported_url("https://music.youtube.com/watch?v=abc123"),
            "https://music.youtube.com/watch?v=abc123",
        )

    def test_extracts_shared_url_from_surrounding_text(self) -> None:
        self.assertEqual(
            _extract_supported_url("Track title\nhttps://youtu.be/abc123?si=share-token."),
            "https://youtu.be/abc123?si=share-token",
        )

    def test_extracts_scheme_less_url(self) -> None:
        self.assertEqual(
            _extract_supported_url("music.youtube.com/watch?v=abc123"),
            "https://music.youtube.com/watch?v=abc123",
        )

    def test_ignores_non_youtube_text(self) -> None:
        self.assertIsNone(_extract_supported_url("https://example.com/watch?v=abc123"))


class FakeMessage:
    def __init__(self, text: str | None = None, caption: str | None = None):
        self.replies: list[str] = []
        self.reply_kwargs: list[dict] = []
        self.text = text
        self.caption = caption
        self.next_message_id = 100

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)


class FakeUpdate:
    def __init__(self, *, user_id: int = 1, chat_id: int = 1, text: str | None = None):
        self.effective_user = SimpleNamespace(id=user_id, username="tester", full_name="Tester")
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_message = FakeMessage(text=text)
        self.callback_query = None


class FakeCallbackQuery:
    def __init__(self, data: str):
        self.data = data
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, *, text: str, show_alert: bool = False):
        self.answers.append((text, show_alert))


class FakeCallbackUpdate(FakeUpdate):
    def __init__(self, data: str, *, user_id: int = 1, chat_id: int = 1):
        super().__init__(user_id=user_id, chat_id=chat_id)
        self.callback_query = FakeCallbackQuery(data)


class FakeWorker:
    def __init__(self):
        self.wake_called = False

    def wake(self):
        self.wake_called = True


class FakeTelegramBot:
    def __init__(self):
        self.edits: list[dict] = []
        self.sends: list[dict] = []
        self.edit_error = None
        self.next_message_id = 900

    async def edit_message_text(self, **kwargs):
        if self.edit_error:
            raise self.edit_error
        self.edits.append(kwargs)
        return SimpleNamespace()

    async def send_message(self, **kwargs):
        self.sends.append(kwargs)
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)


def _keyboard_texts(reply_markup) -> list[list[str]]:
    return [[button.text for button in row] for row in reply_markup.inline_keyboard]


class FakeAuthManager:
    def __init__(self, start_result=None, status=YtMusicAuthStatus.AUTHENTICATED):
        self.start_result = start_result
        self._status = status
        self.reset_called = False

    async def start_auth_flow(self, on_complete):
        return self.start_result

    def status(self):
        return self._status

    async def reset(self):
        self.reset_called = True


class FakeMetadataProvider:
    def __init__(self):
        self.cleared = False

    def clear_client_cache(self):
        self.cleared = True


class TelegramProgressCardTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, db: Database, worker: FakeWorker | None = None):
        worker = worker or FakeWorker()
        service = TelegramBotService(
            config=SimpleNamespace(allowed_telegram_ids=frozenset({1})),
            db=db,
            worker=worker,
        )
        bot = FakeTelegramBot()
        service.application = SimpleNamespace(bot=bot)
        return service, bot, worker

    def _create_job(self, db: Database, *, status: JobStatus = JobStatus.QUEUED):
        job = db.create_job(
            source_url="https://youtube.com/watch?v=video",
            normalized_url="https://music.youtube.com/watch?v=video",
            request_kind=RequestKind.TRACK,
            chat_id=1,
            user_id=1,
            requested_by="tester",
            source_id="video",
        )
        if status is not JobStatus.QUEUED:
            db.update_job(job.id, status=status)
        return db.get_job(job.id)

    async def test_url_submission_sends_progress_card_and_stores_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            worker = FakeWorker()
            service = TelegramBotService(
                config=SimpleNamespace(allowed_telegram_ids=frozenset({1})),
                db=db,
                worker=worker,
            )
            update = FakeUpdate(text="https://music.youtube.com/watch?v=video")

            await service.text_message(update, SimpleNamespace())

            jobs = db.list_recent_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].progress_message_id, 101)
            self.assertTrue(worker.wake_called)
            reply = update.effective_message.replies[-1]
            self.assertIn("<b>Job #1</b>", reply)
            self.assertIn("Status: <b>Queued</b>", reply)
            self.assertEqual(update.effective_message.reply_kwargs[-1]["parse_mode"], "HTML")
            self.assertIsNotNone(update.effective_message.reply_kwargs[-1]["reply_markup"])

    def test_progress_renderer_escapes_html_and_shows_download_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            job = self._create_job(db)
            db.replace_job_items(
                job.id,
                [
                    PreflightItem(
                        item_index=1,
                        source_url="https://music.youtube.com/watch?v=video",
                        normalized_url="https://music.youtube.com/watch?v=video",
                        youtube_video_id="video",
                        playlist_item_id=None,
                        title="<Track & One>",
                        artist="Artist > Name",
                        album=None,
                        metadata={"title": "<Track & One>", "artist": "Artist > Name"},
                    )
                ],
            )
            db.update_job(
                job.id,
                status=JobStatus.DOWNLOADING,
                source_title="A <Source> & Friends",
                current_item_index=1,
                progress_percent=55.0,
                progress_eta_seconds=125,
                progress_speed="2.5MiB/s",
            )

            card = _render_progress_card(db.get_job(job.id), db.get_job_items(job.id))

            self.assertIn("Status: <b>Downloading</b>", card)
            self.assertIn("A &lt;Source&gt; &amp; Friends", card)
            self.assertIn("Artist &gt; Name - &lt;Track &amp; One&gt;", card)
            self.assertIn("[######....] 55.0%", card)
            self.assertIn("ETA: 2m 5s", card)
            self.assertIn("Speed: 2.5MiB/s", card)

    def test_progress_renderer_shows_final_status_details(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            statuses = (
                JobStatus.QUEUED,
                JobStatus.COMPLETED,
                JobStatus.PARTIAL,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            )
            for status in statuses:
                with self.subTest(status=status):
                    job = self._create_job(db, status=status)
                    db.update_job(
                        job.id,
                        result_summary="Imported: 1, duplicates: 0, failed: 0",
                        error_message="Something <failed>" if status is JobStatus.FAILED else None,
                    )
                    card = _render_progress_card(db.get_job(job.id), [])
                    self.assertIn(f"Status: <b>{str(status).replace('_', ' ').title()}</b>", card)
                    if status is JobStatus.FAILED:
                        self.assertIn("Something &lt;failed&gt;", card)

    def test_progress_keyboard_matches_job_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            queued = self._create_job(db)
            failed = self._create_job(db, status=JobStatus.FAILED)
            cancelled = self._create_job(db, status=JobStatus.CANCELLED)
            db.update_job(cancelled.id, cancel_requested=True)

            queued_buttons = _keyboard_texts(_build_progress_keyboard(queued))
            failed_buttons = _keyboard_texts(_build_progress_keyboard(failed))
            cancelled_buttons = _keyboard_texts(_build_progress_keyboard(db.get_job(cancelled.id)))

            self.assertIn("Cancel", queued_buttons[0])
            self.assertIn(["Retry"], failed_buttons)
            self.assertNotIn("Cancel", cancelled_buttons[0])
            self.assertIn(["Retry"], cancelled_buttons)

    async def test_callback_refresh_edits_card_and_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            job = self._create_job(db)
            db.update_job(job.id, progress_message_id=77)
            service, bot, _worker = self._service(db)
            update = FakeCallbackUpdate(f"job:refresh:{job.id}")

            await service.job_callback(update, SimpleNamespace())

            self.assertEqual(bot.edits[0]["message_id"], 77)
            self.assertEqual(update.callback_query.answers[-1], ("Progress refreshed.", False))

    async def test_callback_cancel_updates_queued_job_and_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            job = self._create_job(db)
            db.update_job(job.id, progress_message_id=78)
            service, bot, _worker = self._service(db)
            update = FakeCallbackUpdate(f"job:cancel:{job.id}")

            await service.job_callback(update, SimpleNamespace())

            self.assertEqual(db.get_job(job.id).status, JobStatus.CANCELLED)
            self.assertEqual(bot.edits[0]["message_id"], 78)
            self.assertEqual(update.callback_query.answers[-1], (f"Job #{job.id} cancelled.", False))

    async def test_callback_retry_requeues_job_and_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            job = self._create_job(db, status=JobStatus.FAILED)
            db.update_job(job.id, progress_message_id=79, error_message="failed")
            service, bot, worker = self._service(db)
            update = FakeCallbackUpdate(f"job:retry:{job.id}")

            await service.job_callback(update, SimpleNamespace())

            self.assertEqual(db.get_job(job.id).status, JobStatus.QUEUED)
            self.assertTrue(worker.wake_called)
            self.assertEqual(bot.edits[0]["message_id"], 79)
            self.assertEqual(update.callback_query.answers[-1], (f"Job #{job.id} requeued.", False))

    async def test_not_modified_edit_is_treated_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            job = self._create_job(db)
            db.update_job(job.id, progress_message_id=80)
            service, bot, _worker = self._service(db)
            bot.edit_error = BadRequest("Message is not modified")

            await service.update_progress_card(job.id, True)

            self.assertEqual(bot.sends, [])
            self.assertIn(job.id, service._progress_edit_state)

    async def test_deleted_progress_card_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(Path(tempdir) / "app.db")
            job = self._create_job(db)
            db.update_job(job.id, progress_message_id=81)
            service, bot, _worker = self._service(db)
            bot.edit_error = BadRequest("Message to edit not found")

            await service.update_progress_card(job.id, True)

            self.assertEqual(len(bot.sends), 1)
            self.assertEqual(db.get_job(job.id).progress_message_id, 901)


class YtMusicBotCommandTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, auth, metadata=None):
        return TelegramBotService(
            config=SimpleNamespace(allowed_telegram_ids=frozenset({1})),
            db=SimpleNamespace(),
            worker=SimpleNamespace(),
            ytmusic_auth=auth,
            ytmusic_metadata=metadata,
        )

    async def test_ytmusic_auth_command_replies_with_device_flow(self) -> None:
        flow = YtMusicAuthFlow(
            device_code="device-code",
            user_code="USER-CODE",
            verification_url="https://google.example/device",
            expires_at=9999999999,
            interval_seconds=5,
        )
        auth = FakeAuthManager(YtMusicAuthStartResult(YtMusicAuthStartStatus.STARTED, flow))
        service = self._service(auth)
        update = FakeUpdate()

        await service.ytmusic_auth_command(update, SimpleNamespace())

        reply = update.effective_message.replies[-1]
        self.assertIn("USER-CODE", reply)
        self.assertIn("https://google.example/device?user_code=USER-CODE", reply)

    async def test_ytmusic_auth_status_respects_allowed_gate(self) -> None:
        service = self._service(FakeAuthManager())
        update = FakeUpdate(user_id=99, chat_id=99)

        await service.ytmusic_auth_status_command(update, SimpleNamespace())

        self.assertEqual(update.effective_message.replies[-1], "This bot is restricted to configured Telegram IDs.")

    async def test_ytmusic_auth_reset_clears_provider_cache(self) -> None:
        auth = FakeAuthManager()
        metadata = FakeMetadataProvider()
        service = self._service(auth, metadata)
        update = FakeUpdate()

        await service.ytmusic_auth_reset_command(update, SimpleNamespace())

        self.assertTrue(auth.reset_called)
        self.assertTrue(metadata.cleared)
        self.assertEqual(update.effective_message.replies[-1], "YouTube Music OAuth token reset.")


if __name__ == "__main__":
    unittest.main()
