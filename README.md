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

Optional environment variables:

- `CONFIG_TEMPLATE_DIR` for the directory containing `beets.yaml` and `yt-dlp.conf`. The Docker image sets this to `/app/config`.
- `TELEGRAM_CONNECT_TIMEOUT`, `TELEGRAM_READ_TIMEOUT`, `TELEGRAM_WRITE_TIMEOUT`, and `TELEGRAM_POOL_TIMEOUT` tune Bot API HTTP timeouts in seconds. They default to `30`.
- `TELEGRAM_POLL_TIMEOUT` controls Telegram long-poll duration in seconds. It defaults to `10`.
- `TELEGRAM_BOOTSTRAP_RETRIES` controls startup retries. It defaults to `-1`, which retries indefinitely after Telegram network errors.
- `LOG_LEVEL` controls this app's logs. It defaults to `INFO`.
- `EXTERNAL_LOG_LEVEL` controls noisy dependency logs from Telegram/httpx. It defaults to `WARNING`.

## Local Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
python -m ytmusic_jellyfin_bot
```

The runtime expects `yt-dlp`, `beet`, and `ffmpeg` to be available. The Docker image installs them automatically.

## Container Image

This repo is configured to publish a Docker image to GitHub Container Registry:

- `ghcr.io/lukahummel/youtube-music-downloader:latest` from pushes to `main`
- `ghcr.io/lukahummel/youtube-music-downloader:vX.Y.Z` from git tags like `v0.1.0`

If the package should be pullable without authentication, set the GitHub package visibility to public after the first publish.

## Unraid

Use this image in Unraid:

- `ghcr.io/lukahummel/youtube-music-downloader:latest`

Recommended container paths and variables:

- `/music` mapped to your Jellyfin music library
- `/downloads` mapped to temporary download storage
- `/data` mapped to app state storage
- `/run/secrets/youtube_cookies.txt` mapped read-only if you need private playlists
- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_TELEGRAM_IDS`
- `WORKER_CONCURRENCY`
- `LOG_LEVEL`

The app already defaults to `/music`, `/downloads`, `/data`, and `/run/secrets/youtube_cookies.txt`, so those are the simplest paths to keep in the Unraid template.

## Bot Commands

- `/help`
- `/start`
- `/track <url>`
- `/playlist <url>`
- `/status`
- `/status <job_id>`
- `/jobs`
- `/retry <job_id>`
- `/cancel <job_id>`

Plain text messages containing a supported URL also queue a job.
