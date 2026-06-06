import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ytmusic_jellyfin_bot.beets_runner import BeetsRunner


class BeetsRunnerPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.music_library_dir = self.root / "music"
        self.music_library_dir.mkdir()
        self.beets_library_path = self.root / "beets" / "musiclibrary.db"
        self.beets_library_path.parent.mkdir()
        self.runner = BeetsRunner(
            SimpleNamespace(
                beets_library_path=self.beets_library_path,
                music_library_dir=self.music_library_dir,
            )
        )
        self._create_items_table()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_newly_imported_relative_beets_path_resolves_under_music_library(self) -> None:
        relative_path = Path("JENNIE, Dua Lipa") / "Ruby" / "Handlebars.m4a"
        self._insert_item(
            item_id=1,
            path=str(relative_path),
            title="Handlebars",
            artist="JENNIE, Dua Lipa",
            album="Ruby",
        )

        self.assertEqual(
            self.runner._find_newly_imported_path(0),
            self.music_library_dir / relative_path,
        )

    def test_newly_imported_absolute_beets_path_is_preserved(self) -> None:
        absolute_path = self.root / "external" / "Handlebars.m4a"
        self._insert_item(
            item_id=1,
            path=str(absolute_path),
            title="Handlebars",
            artist="JENNIE, Dua Lipa",
            album="Ruby",
        )

        self.assertEqual(self.runner._find_newly_imported_path(0), absolute_path)

    def test_existing_relative_duplicate_path_resolves_under_music_library(self) -> None:
        relative_path = Path("JENNIE, Dua Lipa") / "Ruby" / "Handlebars.m4a"
        self._insert_item(
            item_id=1,
            path=str(relative_path),
            title="Handlebars",
            artist="JENNIE, Dua Lipa",
            album="Ruby",
        )

        path, reason = self.runner.find_existing_path(
            {
                "track": "Handlebars",
                "artist": "JENNIE, Dua Lipa",
                "album": "Ruby",
            }
        )

        self.assertEqual(path, self.music_library_dir / relative_path)
        self.assertEqual(reason, "metadata")

    def _create_items_table(self) -> None:
        connection = sqlite3.connect(self.beets_library_path)
        try:
            connection.execute(
                """
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    mb_trackid TEXT
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _insert_item(
        self,
        *,
        item_id: int,
        path: str,
        title: str,
        artist: str,
        album: str,
        mb_trackid: str | None = None,
    ) -> None:
        connection = sqlite3.connect(self.beets_library_path)
        try:
            connection.execute(
                """
                INSERT INTO items (id, path, title, artist, album, mb_trackid)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item_id, path, title, artist, album, mb_trackid),
            )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
