# YouTube Music Downloader for Jellyfin

This service accepts YouTube and YouTube Music URLs through a private Telegram bot, downloads audio with `yt-dlp`, retags it with `beets`, and stores the result in a Jellyfin music library.

## Features

- Telegram-only v1 interface with long polling
- Public and private playlist support via mounted `cookies.txt`
- YouTube URL normalization to `music.youtube.com`
- `yt-dlp` download pipeline based on the existing Jellyfin-focused config
- YouTube Music metadata enrichment through `ytmusicapi` with Telegram-driven OAuth setup
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
- `COLOR_LOGS` controls ANSI-colored severity tags in container logs. It defaults to `true`.
- `YTDLP_YOUTUBE_PLAYER_CLIENTS` controls the YouTube player-client probe order. It defaults to `default,web_embedded,web_safari,mweb,web`.
- `YTDLP_YOUTUBE_EXTRACTOR_ARGS` appends YouTube extractor args to format probes and downloads. Use this for PO-token settings when YouTube requires them for your cookie session; for example, pair `YTDLP_YOUTUBE_PLAYER_CLIENTS=mweb` with a matching `po_token=...` value.
- `YTMUSIC_METADATA_ENABLED` controls YouTube Music metadata enrichment. It defaults to `true`.
- `YTMUSIC_OAUTH_CLIENT_ID` and `YTMUSIC_OAUTH_CLIENT_SECRET` enable authenticated `ytmusicapi` requests through `/ytmusic_auth`.
- `YTMUSIC_OAUTH_FILE` stores the OAuth token JSON. It defaults to `/data/ytmusic/oauth.json`.
- `YTMUSIC_LANGUAGE`, `YTMUSIC_LOCATION`, and `YTMUSIC_REQUEST_TIMEOUT` tune `ytmusicapi` requests. They default to `en`, empty/default location, and `10` seconds.
- `YTMUSIC_FETCH_LYRICS`, `YTMUSIC_FETCH_CREDITS`, and `YTMUSIC_EMBED_ARTWORK` control optional enriched tags. They default to `true`.

## YouTube Music Metadata OAuth

The bot can enrich downloaded files with YouTube Music-native title, artists, album, year, lyrics, credits-derived composer tags, and artwork. It starts without blocking even when OAuth is not configured; unauthenticated enrichment is used until a saved OAuth token is available.

To enable authenticated enrichment:

1. In Google Cloud, create an OAuth client ID for **TVs and Limited Input devices**.
2. Enable the YouTube Data API for that project.
3. Set `YTMUSIC_OAUTH_CLIENT_ID` and `YTMUSIC_OAUTH_CLIENT_SECRET` in the container environment.
4. Start the bot and send `/ytmusic_auth` from an allowed Telegram account.
5. Open the verification URL shown by the bot and enter the displayed code.
6. Confirm `/data/ytmusic/oauth.json` exists in the mounted `/data` volume. Restarts reuse this file.

Use `/ytmusic_auth_status` to inspect auth state and `/ytmusic_auth_reset` to remove the saved token and start over. The bot never prints OAuth tokens or the client secret in logs or Telegram messages.

## Local Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
python -m ytmusic_jellyfin_bot
```

The runtime expects `yt-dlp`, `yt-dlp-ejs`, `beet`, `ffmpeg`/`ffprobe`, a supported YouTube JavaScript runtime, and the yt-dlp networking/metadata extras to be available. The Docker image installs Deno, AtomicParsley, ffmpeg/ffprobe, and `yt-dlp[default,curl-cffi]` automatically.

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
- `YTDLP_YOUTUBE_EXTRACTOR_ARGS` only if YouTube requires a PO token for your cookie session
- `YTMUSIC_OAUTH_CLIENT_ID` and `YTMUSIC_OAUTH_CLIENT_SECRET` if you want authenticated YouTube Music metadata enrichment

The app already defaults to `/music`, `/downloads`, `/data`, and `/run/secrets/youtube_cookies.txt`, so those are the simplest paths to keep in the Unraid template. The `/data` mount must be persistent so `/data/ytmusic/oauth.json` survives restarts.

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
- `/ytmusic_auth`
- `/ytmusic_auth_status`
- `/ytmusic_auth_reset`

Plain text messages containing a supported URL also queue a job.
