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


if __name__ == "__main__":
    unittest.main()
