import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.ytmusic_auth import (
    YtMusicAuthManager,
    YtMusicAuthStartStatus,
    YtMusicAuthStatus,
)


class FakeCredentials:
    instances: list["FakeCredentials"] = []
    token_responses: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeCredentials.instances.append(self)

    def get_code(self) -> dict:
        return {
            "device_code": "device-code",
            "user_code": "USER-CODE",
            "verification_url": "https://google.example/device",
            "expires_in": 60,
            "interval": 1,
        }

    def token_from_code(self, device_code: str) -> dict:
        return FakeCredentials.token_responses.pop(0)


def _config(path: Path, **overrides):
    values = {
        "ytmusic_metadata_enabled": True,
        "ytmusic_oauth_client_id": "client-id",
        "ytmusic_oauth_client_secret": "client-secret",
        "ytmusic_oauth_file": path,
        "ytmusic_request_timeout": 10.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class YtMusicAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeCredentials.instances = []
        FakeCredentials.token_responses = []
        self.tempdir = tempfile.TemporaryDirectory()
        self.oauth_file = Path(self.tempdir.name) / "oauth.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_missing_client_credentials_reports_status(self) -> None:
        manager = YtMusicAuthManager(
            _config(self.oauth_file, ytmusic_oauth_client_id=None),
            oauth_credentials_factory=FakeCredentials,
        )

        result = await manager.start_auth_flow(lambda message: asyncio.sleep(0))

        self.assertEqual(result.status, YtMusicAuthStartStatus.MISSING_CLIENT_CREDENTIALS)
        self.assertEqual(manager.status(), YtMusicAuthStatus.MISSING_CLIENT_CREDENTIALS)

    async def test_successful_flow_writes_token_atomically(self) -> None:
        FakeCredentials.token_responses = [
            {"error": "authorization_pending"},
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scope": "https://www.googleapis.com/auth/youtube",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        ]
        messages: list[str] = []
        manager = YtMusicAuthManager(_config(self.oauth_file), oauth_credentials_factory=FakeCredentials)

        async def no_sleep(seconds: float) -> None:
            return None

        with mock.patch("ytmusic_jellyfin_bot.ytmusic_auth.asyncio.sleep", no_sleep):
            result = await manager.start_auth_flow(lambda message: _append_message(messages, message))
            self.assertEqual(result.status, YtMusicAuthStartStatus.STARTED)
            task = manager._active_task
            assert task is not None
            await task

        token = json.loads(self.oauth_file.read_text(encoding="utf-8"))
        self.assertEqual(token["access_token"], "access-token")
        self.assertEqual(token["refresh_token"], "refresh-token")
        self.assertEqual(manager.status(), YtMusicAuthStatus.AUTHENTICATED)
        self.assertIn("completed", messages[-1])
        self.assertNotIn("client-secret", "\n".join(messages))

    async def test_concurrent_start_reuses_active_flow(self) -> None:
        FakeCredentials.token_responses = [{"error": "authorization_pending"}]
        manager = YtMusicAuthManager(_config(self.oauth_file), oauth_credentials_factory=FakeCredentials)

        first = await manager.start_auth_flow(lambda message: asyncio.sleep(0))
        second = await manager.start_auth_flow(lambda message: asyncio.sleep(0))

        self.assertEqual(first.status, YtMusicAuthStartStatus.STARTED)
        self.assertEqual(second.status, YtMusicAuthStartStatus.ALREADY_IN_PROGRESS)
        self.assertEqual(first.flow, second.flow)
        await manager.reset()

    async def test_expired_or_denied_flow_does_not_write_token(self) -> None:
        FakeCredentials.token_responses = [{"error": "access_denied"}]
        messages: list[str] = []
        manager = YtMusicAuthManager(_config(self.oauth_file), oauth_credentials_factory=FakeCredentials)

        async def no_sleep(seconds: float) -> None:
            return None

        with mock.patch("ytmusic_jellyfin_bot.ytmusic_auth.asyncio.sleep", no_sleep):
            await manager.start_auth_flow(lambda message: _append_message(messages, message))
            task = manager._active_task
            assert task is not None
            await task

        self.assertFalse(self.oauth_file.exists())
        self.assertIn("failed", messages[-1])

    async def test_reset_removes_saved_token(self) -> None:
        self.oauth_file.write_text("{}", encoding="utf-8")
        manager = YtMusicAuthManager(_config(self.oauth_file), oauth_credentials_factory=FakeCredentials)

        await manager.reset()

        self.assertFalse(self.oauth_file.exists())
        self.assertEqual(manager.status(), YtMusicAuthStatus.NOT_AUTHENTICATED)


async def _append_message(messages: list[str], message: str) -> None:
    messages.append(message)


if __name__ == "__main__":
    unittest.main()
