import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ytmusic_jellyfin_bot.config as config_module
from ytmusic_jellyfin_bot.config import AppConfig, _resolve_config_template_dir


class ConfigTests(unittest.TestCase):
    def test_resolves_templates_from_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            app_dir = temp_path / "app"
            config_dir = app_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "beets.yaml").write_text("directory: /music\n", encoding="utf-8")
            (config_dir / "yt-dlp.conf").write_text("-x\n", encoding="utf-8")

            installed_project_root = temp_path / "usr" / "local" / "lib" / "python3.12"
            with mock.patch.dict(os.environ, {}, clear=True):
                resolved = _resolve_config_template_dir(installed_project_root, cwd=app_dir)

            self.assertEqual(resolved, config_dir.resolve())

    def test_resolves_bundled_templates_as_installed_package_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            installed_project_root = temp_path / "usr" / "local" / "lib" / "python3.12"
            unrelated_cwd = temp_path / "workspace"
            unrelated_cwd.mkdir()

            with mock.patch.dict(os.environ, {}, clear=True):
                resolved = _resolve_config_template_dir(installed_project_root, cwd=unrelated_cwd)

            expected = Path(config_module.__file__).resolve().parent / "config"
            self.assertEqual(resolved, expected.resolve())

    def test_bundled_templates_match_root_templates(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        package_config_dir = Path(config_module.__file__).resolve().parent / "config"

        for filename in ("beets.yaml", "yt-dlp.conf"):
            with self.subTest(filename=filename):
                self.assertEqual(
                    (repo_root / "config" / filename).read_text(encoding="utf-8"),
                    (package_config_dir / filename).read_text(encoding="utf-8"),
                )

    def test_ytdlp_template_does_not_inject_thumbnail_crop_filter(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        config_text = (repo_root / "config" / "yt-dlp.conf").read_text(encoding="utf-8")

        self.assertIn("--convert-thumbnails", config_text)
        self.assertIn("--embed-thumbnail", config_text)
        self.assertNotIn("ThumbnailsConvertor+ffmpeg_o", config_text)

    def test_ytdlp_subtitle_language_template_uses_valid_regex(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        config_text = (repo_root / "config" / "yt-dlp.conf").read_text(encoding="utf-8")
        config_lines = config_text.splitlines()
        subtitle_language_value = config_lines[config_lines.index("--sub-langs") + 1]

        self.assertEqual(subtitle_language_value, ".*orig,-live_chat")

    def test_from_env_uses_config_template_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            config_dir = temp_path / "templates"
            config_dir.mkdir()
            (config_dir / "beets.yaml").write_text(
                "directory: {{MUSIC_LIBRARY_DIR}}\n"
                "library: {{APP_STATE_DIR}}/beets/musiclibrary.db\n",
                encoding="utf-8",
            )
            (config_dir / "yt-dlp.conf").write_text(
                "--paths\n{{STAGING_DIR}}\n",
                encoding="utf-8",
            )

            app_state_dir = temp_path / "data"
            music_dir = temp_path / "music"
            staging_dir = temp_path / "downloads"
            env = {
                "TELEGRAM_BOT_TOKEN": "123456:telegram-token",
                "ALLOWED_TELEGRAM_IDS": "123456789",
                "MUSIC_LIBRARY_DIR": str(music_dir),
                "STAGING_DIR": str(staging_dir),
                "APP_STATE_DIR": str(app_state_dir),
                "CONFIG_TEMPLATE_DIR": str(config_dir),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env()

            config.prepare_runtime()

            self.assertEqual(config.config_template_dir, config_dir.resolve())
            self.assertEqual(config.telegram_connect_timeout, 30.0)
            self.assertEqual(config.telegram_read_timeout, 30.0)
            self.assertEqual(config.telegram_write_timeout, 30.0)
            self.assertEqual(config.telegram_pool_timeout, 30.0)
            self.assertEqual(config.telegram_poll_timeout, 10)
            self.assertEqual(config.telegram_bootstrap_retries, -1)
            self.assertEqual(config.log_level, "INFO")
            self.assertEqual(config.external_log_level, "WARNING")
            self.assertTrue(config.color_logs)
            self.assertEqual(
                config.youtube_player_clients,
                ("default", "web_embedded", "web_safari", "mweb", "web"),
            )
            self.assertIsNone(config.youtube_extractor_args)
            self.assertTrue(config.ytmusic_metadata_enabled)
            self.assertIsNone(config.ytmusic_oauth_client_id)
            self.assertIsNone(config.ytmusic_oauth_client_secret)
            self.assertEqual(config.ytmusic_oauth_file, app_state_dir.resolve() / "ytmusic" / "oauth.json")
            self.assertEqual(config.ytmusic_language, "en")
            self.assertEqual(config.ytmusic_location, "")
            self.assertEqual(config.ytmusic_request_timeout, 10.0)
            self.assertTrue(config.ytmusic_fetch_lyrics)
            self.assertTrue(config.ytmusic_fetch_credits)
            self.assertTrue(config.ytmusic_embed_artwork)
            self.assertIn(
                f"directory: {music_dir.resolve().as_posix()}",
                config.runtime_beets_config_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                staging_dir.resolve().as_posix(),
                config.runtime_ytdlp_config_path.read_text(encoding="utf-8"),
            )

    def test_from_env_uses_telegram_network_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            config_dir = temp_path / "templates"
            config_dir.mkdir()
            (config_dir / "beets.yaml").write_text("directory: /music\n", encoding="utf-8")
            (config_dir / "yt-dlp.conf").write_text("-x\n", encoding="utf-8")

            env = {
                "TELEGRAM_BOT_TOKEN": "123456:telegram-token",
                "ALLOWED_TELEGRAM_IDS": "123456789",
                "CONFIG_TEMPLATE_DIR": str(config_dir),
                "TELEGRAM_CONNECT_TIMEOUT": "45.5",
                "TELEGRAM_READ_TIMEOUT": "60",
                "TELEGRAM_WRITE_TIMEOUT": "61",
                "TELEGRAM_POOL_TIMEOUT": "62",
                "TELEGRAM_POLL_TIMEOUT": "20",
                "TELEGRAM_BOOTSTRAP_RETRIES": "5",
                "LOG_LEVEL": "debug",
                "EXTERNAL_LOG_LEVEL": "error",
                "COLOR_LOGS": "false",
                "YTDLP_YOUTUBE_PLAYER_CLIENTS": "mweb,web",
                "YTDLP_YOUTUBE_EXTRACTOR_ARGS": "po_token=mweb.gvs+token",
                "YTMUSIC_METADATA_ENABLED": "false",
                "YTMUSIC_OAUTH_CLIENT_ID": "client-id",
                "YTMUSIC_OAUTH_CLIENT_SECRET": "client-secret",
                "YTMUSIC_OAUTH_FILE": str(temp_path / "oauth.json"),
                "YTMUSIC_LANGUAGE": "de",
                "YTMUSIC_LOCATION": "DE",
                "YTMUSIC_REQUEST_TIMEOUT": "4.5",
                "YTMUSIC_FETCH_LYRICS": "false",
                "YTMUSIC_FETCH_CREDITS": "false",
                "YTMUSIC_EMBED_ARTWORK": "false",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env()

            self.assertEqual(config.telegram_connect_timeout, 45.5)
            self.assertEqual(config.telegram_read_timeout, 60.0)
            self.assertEqual(config.telegram_write_timeout, 61.0)
            self.assertEqual(config.telegram_pool_timeout, 62.0)
            self.assertEqual(config.telegram_poll_timeout, 20)
            self.assertEqual(config.telegram_bootstrap_retries, 5)
            self.assertEqual(config.log_level, "DEBUG")
            self.assertEqual(config.external_log_level, "ERROR")
            self.assertFalse(config.color_logs)
            self.assertEqual(config.youtube_player_clients, ("mweb", "web"))
            self.assertEqual(config.youtube_extractor_args, "po_token=mweb.gvs+token")
            self.assertFalse(config.ytmusic_metadata_enabled)
            self.assertEqual(config.ytmusic_oauth_client_id, "client-id")
            self.assertEqual(config.ytmusic_oauth_client_secret, "client-secret")
            self.assertEqual(config.ytmusic_oauth_file, (temp_path / "oauth.json").resolve())
            self.assertEqual(config.ytmusic_language, "de")
            self.assertEqual(config.ytmusic_location, "DE")
            self.assertEqual(config.ytmusic_request_timeout, 4.5)
            self.assertFalse(config.ytmusic_fetch_lyrics)
            self.assertFalse(config.ytmusic_fetch_credits)
            self.assertFalse(config.ytmusic_embed_artwork)


if __name__ == "__main__":
    unittest.main()
