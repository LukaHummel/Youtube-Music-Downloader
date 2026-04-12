# YouTube Music Downloader for Jellyfin

This service accepts YouTube and YouTube Music URLs through a private Telegram bot, downloads audio with `yt-dlp`, retags it with `beets`, and stores the result in a Jellyfin music library.

## Features

- Telegram-only v1 interface with long polling
- Public and private playlist support via mounted `cookies.txt`
- YouTube URL normalization to `music.youtube.com`
- `yt-dlp` download pipeline based on the existing Jellyfin-focused config
- `beets` singleton imports into `Artist/Album` or `Artist/Singles`
- `.m3u8` playlist generation for playlist jobs
- SQLite-backed queue, job history, and source-path mappings

## Environment

Required environment variables:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_TELEGRAM_IDS`
- `MUSIC_LIBRARY_DIR`
- `STAGING_DIR`
- `APP_STATE_DIR`
- `YTDLP_COOKIES_FILE`
- `WORKER_CONCURRENCY`

See [`.env.example`](./.env.example) and [`docker-compose.example.yml`](./docker-compose.example.yml).

## Local Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
python -m ytmusic_jellyfin_bot
```

The runtime expects `yt-dlp`, `beet`, and `ffmpeg` to be available. The Docker image installs them automatically.

## Bot Commands

- `/help`
- `/track <url>`
- `/playlist <url>`
- `/status`
- `/status <job_id>`
- `/jobs`
- `/retry <job_id>`
- `/cancel <job_id>`

Plain text messages containing a supported URL also queue a job.
