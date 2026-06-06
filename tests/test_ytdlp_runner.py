import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.models import RequestKind
from ytmusic_jellyfin_bot.ytdlp_runner import FormatSelection, YtDlpError, YtDlpRunner


class FakeYtDlpRunner(YtDlpRunner):
    def __init__(
        self,
        result: tuple[str, str, int] | list[tuple[str, str, int]],
        *,
        cookies_available: bool = True,
    ):
        config = SimpleNamespace(
            runtime_ytdlp_config_path=Path("yt-dlp.conf"),
            ytdlp_archive_path=Path("/data/yt-dlp-archive.txt"),
            cookies_available=cookies_available,
            ytdlp_cookies_file=Path("/run/secrets/youtube_cookies.txt"),
        )
        super().__init__(config)
        self.results = result if isinstance(result, list) else [result]
        self.command: list[str] | None = None
        self.commands: list[list[str]] = []

    async def _run_capture(self, command: list[str]) -> tuple[str, str, int]:
        self.command = command
        self.commands.append(command)
        return self.results.pop(0)


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
        self.assertIn("--ignore-no-formats-error", runner.command)
        self.assertIn("--cookies", runner.command)
        self.assertNotIn("--config-locations", runner.command)
        self.assertNotIn("--format", runner.command)

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

    async def test_preflight_requires_cookies_file(self) -> None:
        runner = FakeYtDlpRunner((json.dumps({}), "", 0), cookies_available=False)

        with self.assertRaises(YtDlpError) as caught:
            await runner.preflight(
                "https://music.youtube.com/watch?v=ADUis5-M15Y",
                RequestKind.TRACK,
            )

        self.assertTrue(caught.exception.auth_required)
        self.assertIn("Required cookies.txt file", str(caught.exception))

    def test_download_command_uses_cookies_and_format_selector(self) -> None:
        runner = FakeYtDlpRunner((json.dumps({}), "", 0))
        selection = FormatSelection(
            format_id="251",
            player_client=None,
            ext="webm",
            acodec="opus",
            vcodec="none",
            abr=160.0,
            tbr=160.0,
        )

        command = runner._download_command(
            item_dir=Path("/downloads/1/0001"),
            url="https://music.youtube.com/watch?v=ADUis5-M15Y",
            selection=selection,
        )

        self.assertIn("--cookies", command)
        self.assertEqual(command[command.index("--format") + 1], "251")
        self.assertNotIn("--extractor-args", command)

    def test_download_command_explicit_player_client_keeps_cookies(self) -> None:
        runner = FakeYtDlpRunner((json.dumps({}), "", 0))
        selection = FormatSelection(
            format_id="251",
            player_client="android_vr",
            ext="webm",
            acodec="opus",
            vcodec="none",
            abr=160.0,
            tbr=160.0,
        )

        command = runner._download_command(
            item_dir=Path("/downloads/1/0001"),
            url="https://music.youtube.com/watch?v=ADUis5-M15Y",
            selection=selection,
        )

        self.assertIn("--cookies", command)
        self.assertEqual(command[command.index("--extractor-args") + 1], "youtube:player_client=android_vr")

    async def test_resolve_download_format_selects_best_audio_format(self) -> None:
        payload = {
            "formats": [
                {"format_id": "18", "acodec": "mp4a.40.2", "vcodec": "avc1", "abr": None, "tbr": 603},
                {"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none", "abr": 128, "tbr": 128},
                {"format_id": "251", "acodec": "opus", "vcodec": "none", "abr": 160, "tbr": 160},
            ]
        }
        runner = FakeYtDlpRunner((json.dumps(payload), "", 0))

        selection = await runner._resolve_download_format("https://music.youtube.com/watch?v=ADUis5-M15Y")

        self.assertEqual(selection.format_id, "251")
        self.assertIsNone(selection.player_client)

    async def test_resolve_download_format_tries_next_client_when_no_formats_exist(self) -> None:
        empty_payload = {"formats": []}
        audio_payload = {
            "formats": [
                {"format_id": "251", "acodec": "opus", "vcodec": "none", "abr": 160, "tbr": 160}
            ]
        }
        runner = FakeYtDlpRunner(
            [(json.dumps(empty_payload), "", 0), (json.dumps(audio_payload), "", 0)]
        )

        selection = await runner._resolve_download_format("https://music.youtube.com/watch?v=ADUis5-M15Y")

        self.assertEqual(selection.format_id, "251")
        self.assertEqual(selection.player_client, "android_vr")


if __name__ == "__main__":
    unittest.main()
