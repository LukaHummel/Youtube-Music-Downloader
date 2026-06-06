import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ytmusic_jellyfin_bot.main as main_module


class MainTests(unittest.TestCase):
    def test_dependency_versions_handles_missing_metadata(self) -> None:
        def fake_package_version(package_name: str) -> str:
            if package_name == "beets":
                raise main_module.PackageNotFoundError(package_name)
            return f"{package_name}-version"

        with mock.patch.object(main_module, "package_version", fake_package_version):
            versions = main_module._dependency_versions()

        self.assertEqual(
            versions,
            "yt-dlp=yt-dlp-version yt-dlp-ejs=yt-dlp-ejs-version "
            "curl-cffi=curl-cffi-version brotli=brotli-version certifi=certifi-version "
            "mutagen=mutagen-version pycryptodomex=pycryptodomex-version "
            "requests=requests-version urllib3=urllib3-version websockets=websockets-version "
            "beets=not installed "
            "python-telegram-bot=python-telegram-bot-version",
        )


if __name__ == "__main__":
    unittest.main()
