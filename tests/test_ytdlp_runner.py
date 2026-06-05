import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.models import RequestKind
from ytmusic_jellyfin_bot.ytdlp_runner import YtDlpError, YtDlpRunner


class FakeYtDlpRunner(YtDlpRunner):
    def __init__(self, result: tuple[str, str, int]):
        config = SimpleNamespace(
            runtime_ytdlp_config_path=Path("yt-dlp.conf"),
            cookies_available=False,
        )
        super().__init__(config)
        self.result = result
        self.command: list[str] | None = None

    async def _run_capture(self, command: list[str]) -> tuple[str, str, int]:
        self.command = command
        return self.result


class YtDlpRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_track_preflight_accepts_public_music_category_video(self) -> None:
        payload = {
            "id": "ADUis5-M15Y",
            "title": "sma$her & MXZI - ACELERADA | Car Music",
            "channel": "Niza",
            "categories": ["Music"],
        }
        runner = FakeYtDlpRunner((json.dumps(payload), "", 0))

        result = await runner.preflight(
            "https://music.youtube.com/watch?v=ADUis5-M15Y",
            RequestKind.TRACK,
        )

        self.assertEqual(result.source_id, "ADUis5-M15Y")
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].youtube_video_id, "ADUis5-M15Y")
        self.assertEqual(result.items[0].artist, "Niza")
        self.assertIsNotNone(runner.command)
        assert runner.command is not None
        self.assertIn("--ignore-config", runner.command)
        self.assertNotIn("--config-locations", runner.command)

    async def test_preflight_auth_failure_preserves_ytdlp_output(self) -> None:
        output = "ERROR: [youtube] ADUis5-M15Y: Sign in to confirm you're not a bot. Use --cookies."
        runner = FakeYtDlpRunner(("", output, 1))

        with self.assertRaises(YtDlpError) as caught:
            await runner.preflight(
                "https://music.youtube.com/watch?v=ADUis5-M15Y",
                RequestKind.TRACK,
            )

        self.assertTrue(caught.exception.auth_required)
        self.assertEqual(caught.exception.output, output)


if __name__ == "__main__":
    unittest.main()
