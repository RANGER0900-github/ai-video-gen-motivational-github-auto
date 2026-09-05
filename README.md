# AI Motivational Video Creator
# AI Motivational Video Generator (GitHub Actions Auto-Upload)

Linux-first motivational video generator controlled entirely through a Telegram bot. The bot manages generation, looping, listing, delivery, and restart recovery while the existing SQLite-backed render queue keeps jobs alive across process restarts.
An automated, serverless motivational video generator and YouTube Shorts publishing pipeline powered by GitHub Actions.

## What It Does
## How It Works

- generates vertical motivational videos from local quotes, images, music, and fonts
- controls the full workflow from Telegram with `/start`, `/generate_video`, `/video_loop`, `/list`, `/status`, and `/stop`
- sends completed videos to Telegram as `sendVideo` uploads
- can auto-post loop-generated videos to YouTube and send the YouTube URL back to Telegram
- keeps a persistent JSON ledger for YouTube upload state, retries, quota blocking, and renamed files
- keeps loop mode alive across app restarts and machine reboots
- supports Linux autostart with `systemd`, `cron @reboot`, or shell fallback
1. **Scheduled Trigger**: GitHub Actions runs on a cron schedule 6 times a day (spaced 2 hours apart between 08:00 and 18:00 UTC) to match the free YouTube Data API quota limit. It can also be triggered on-demand via `workflow_dispatch` in the GitHub Actions UI.
2. **Video Rendering**: Python selects an unused quote from `quotes.csv` and the least-used background image from `images_usage.json`. Pillow renders smooth, wrapped, faded text overlays, and FFmpeg pairs them with background music into a crisp 1080x1920 (9:16 portrait) video.
3. **YouTube Shorts Upload**: Node.js (`upload.js`) uploads the generated video directly to YouTube with an auto-generated title derived from the quote, rich descriptions, and trending motivational hashtags.
4. **State Persistence**: GitHub Actions automatically commits and pushes the updated `quotes.csv` (with the quote marked as `used`) and `images_usage.json` (with updated image counters) back to the repository with `[skip ci]`.
5. **No Bloat**: Temporary video files in `outputs/` are discarded when the runner stops, keeping your git repository lean and fast.

## Stack
## Quota & Scheduling

- Runtime: Python 3.11+
- Bot layer: `python-telegram-bot`
- Queue and persistence: SQLite + background worker thread
- Rendering: Pillow + FFmpeg
The free tier of the YouTube Data API provides **10,000 units per day**. Each video upload costs **1,600 units**, allowing a maximum of **6 video uploads per day** ($10,000 / 1,600 = 6$).

## Project Layout

```text
backend/         queue, renderer, Telegram bot runtime, storage, models
images/          source background images
music/           source music tracks
fonts/           source font files
quotes.csv       quote library used for generation
outputs/         generated videos (local only, gitignored)
state/           SQLite job/event/bot database (local only, gitignored)
                 youtube_queue.json for upload backlog/quota tracking
scripts/         run, background start, and autostart helper scripts
deploy/          systemd unit and cron example
The workflow schedule in `.github/workflows/auto_upload.yml`:
```yaml
schedule:
  # Runs 6 times a day: 08:00, 10:00, 12:00, 14:00, 16:00, 18:00 UTC
  - cron: '0 8,10,12,14,16,18 * * *'
```

## Environment
*Alternative (Every 4 hours all day)*: `'0 0,4,8,12,16,20 * * *'`

Set these before running the bot:
## GitHub Secrets

```bash
export AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN="your_bot_token"
export AI_VIDEO_GEN_ALLOWED_CHAT_IDS="1702319284"
export AI_VIDEO_GEN_DEFAULT_CHAT_ID="1702319284"
```
Add these three secrets under **Settings > Secrets and variables > Actions**:

For unattended startup, you can instead write them into `state/bot.env` or `.env`. The runner loads both automatically.
| Secret Name | Description |
|---|---|
| `YOUTUBE_CLIENT_ID` | OAuth 2.0 Client ID from Google Cloud Console |
| `YOUTUBE_CLIENT_SECRET` | OAuth 2.0 Client Secret from Google Cloud Console |
| `YOUTUBE_REFRESH_TOKEN` | OAuth 2.0 Refresh Token with `youtube.upload` scope |

Optional:

### Generating Your Refresh Token
Run the included token helper:
```bash
export AI_VIDEO_GEN_TELEGRAM_PARSE_MODE="HTML"
export AI_VIDEO_GEN_SEND_RETRIES="3"
export AI_VIDEO_GEN_LOOP_BACKOFF_SECONDS="10"
export YOUTUBE_PRIVACY_STATUS="public"
export YOUTUBE_CATEGORY_ID="22"
export AI_VIDEO_GEN_YOUTUBE_RETRY_LIMIT="5"
node scripts/get_youtube_token.js
```
Follow the URL prompt to authorize and paste back the authorization code.

YouTube uploads require OAuth credentials via environment variables:
## Project Structure

```bash
export YOUTUBE_CLIENT_ID="..."
export YOUTUBE_CLIENT_SECRET="..."
export YOUTUBE_REFRESH_TOKEN="..."
```text
.github/workflows/
  auto_upload.yml       # GitHub Actions scheduled workflow
backend/
  app/
    cli.py              # CLI entry point for batch/single video generation
    config.py           # App configuration and paths
    csv_store.py        # quotes.csv reader, writer, and selection logic
    database.py         # SQLite job and event database
    jobs.py             # Render queue and worker service
    models.py           # Pydantic data models
    renderer.py         # Pillow + FFmpeg video generation engine
    storage.py          # Asset discovery and images_usage.json tracking
images/                 # Background portrait images
music/                  # Background audio tracks
fonts/                  # Font files (NotoSans, PlayfairDisplay)
quotes.csv              # Quote library (auto-updated with usage timestamps)
images_usage.json       # Usage counter for balanced image rotation
upload.js               # Node.js YouTube OAuth2 uploader
scripts/
  get_youtube_token.js  # Helper to generate YouTube OAuth refresh token
```

## Requirements
## Local Testing

- Python 3.11+
- FFmpeg available on `PATH`

Check FFmpeg:

### 1. Install Dependencies
```bash
ffmpeg -version
```

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pip install -e .
npm install
```

## One-Command Install

Use the guided installer to collect your bot details, install dependencies, and configure autostart:

### 2. Generate a Test Video
```bash
chmod +x installer.sh
./installer.sh
python -m app.cli --count 1
```
The rendered video will appear in `outputs/`.

The installer:

- detects the Linux package manager and installs Python, FFmpeg, Node.js, npm, and fallback tools
- asks for bot token, chat IDs, and whether the bot should start on boot
- writes persistent config into `state/bot.env`
- chooses the best autostart method in this order: `systemd -> crontab -> screen via rc.local -> manual nohup`
- can start the bot immediately after setup

## Run The Bot

### 3. Test YouTube Upload
Set your credentials in `.env` (gitignored) or shell:
```bash
source .venv/bin/activate
python -m app.telegram_bot
```
export YOUTUBE_CLIENT_ID="..."
export YOUTUBE_CLIENT_SECRET="..."
export YOUTUBE_REFRESH_TOKEN="..."

Or via the wrapper:

```bash
./scripts/run_bot.sh
node upload.js
```

Detached helpers:

```bash
./scripts/start_bot_background.sh
./scripts/start_bot_screen.sh
```

## Telegram Commands

- `/start` : show the control panel and status
- `/generate_video` : choose 1, 3, 5, 10, or a custom count
- generated Telegram videos include a `📺 Upload to YouTube` button for one-tap posting
- `/video_loop` : keep generating and sending videos forever
- `/video_loop` asks whether loop videos should also auto-post to YouTube
- `/list` : browse completed videos and send one, a page, or all
- `/status` : show loop state, queue state, and recent output
- `/stop` : stop the loop and cancel loop-owned work

## How The Bot Works

1. A Telegram command or button queues one or more jobs.
2. Jobs are stored in SQLite with chat, origin, and delivery metadata.
3. The render worker processes one job at a time.
4. Completed jobs are delivered to Telegram by the bot background loop.
5. If YouTube auto-post is enabled for loop mode, delivered loop videos are queued in `state/youtube_queue.json`.
6. The YouTube uploader posts one video at a time, renames successful files to `*_yt-done.mp4`, and sends the YouTube URL back to Telegram.
7. If YouTube quota is exhausted, uploads pause until the next Pacific-day reset and pending videos stay queued locally.
8. On restart, pending jobs, loop state, and pending YouTube uploads are recovered automatically.

## Autostart

### systemd

Use the provided unit:

```bash
sudo cp deploy/ai-video-gen-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-video-gen-bot.service
```

Edit the unit first if your project path is different.

### cron `@reboot`

Use the example from `deploy/crontab.example`:

```bash
crontab -e
```

Then add:

```bash
@reboot cd /home/meet/projects/ai-video-gen && /home/meet/projects/ai-video-gen/scripts/start_bot_background.sh
```

### Shell fallback

```bash
./scripts/start_bot_background.sh
```

Helper:

```bash
./scripts/install_autostart.sh
```

## Notes

- `quotes.csv` is the quote source of truth
- generated outputs are intentionally not committed
- SQLite runtime state is intentionally not committed
- the bot only allows configured chat IDs
- the Telegram bot token must not be committed

## GitHub

Repository target:

```text
https://github.com/RANGER0900-github/ai-video-gen-motivational-github-auto.git
```
