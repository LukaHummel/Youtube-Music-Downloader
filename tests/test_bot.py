import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.bot import TelegramBotService, _extract_supported_url
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
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, *, user_id: int = 1, chat_id: int = 1):
        self.effective_user = SimpleNamespace(id=user_id, username="tester", full_name="Tester")
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_message = FakeMessage()


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
