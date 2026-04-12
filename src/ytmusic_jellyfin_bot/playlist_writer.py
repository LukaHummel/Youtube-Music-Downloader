from __future__ import annotations

import re
from pathlib import Path

from .config import AppConfig


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PlaylistWriter:
    def __init__(self, config: AppConfig):
        self.config = config

    def write_playlist(self, *, playlist_title: str, playlist_id: str, track_paths: list[Path]) -> Path:
        safe_title = _sanitize_component(playlist_title or "Playlist")
        playlist_path = self.config.playlist_dir / f"{safe_title} [{playlist_id}].m3u8"
        lines = ["#EXTM3U"]
        for track_path in track_paths:
            relative = Path(track_path).resolve().relative_to(self.config.music_library_dir.resolve())
            lines.append(str(Path("..") / relative).replace("\\", "/"))
        playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return playlist_path


def _sanitize_component(value: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", value).strip()
    return cleaned or "Playlist"
