# AI Motivational Video Creator

Linux-first motivational video generator controlled entirely through a Telegram bot. The bot manages generation, looping, listing, delivery, and restart recovery while the existing SQLite-backed render queue keeps jobs alive across process restarts.

## What It Does

- generates vertical motivational videos from local quotes, images, music, and fonts
- controls the full workflow from Telegram with `/start`, `/generate_video`, `/video_loop`, `/list`, `/status`, and `/stop`
- sends completed videos to Telegram as `sendVideo` uploads
- can auto-post loop-generated videos to YouTube and send the YouTube URL back to Telegram
- keeps a persistent JSON ledger for YouTube upload state, retries, quota blocking, and renamed files
- keeps loop mode alive across app restarts and machine reboots
- supports Linux autostart with `systemd`, `cron @reboot`, or shell fallback

## Stack

- Runtime: Python 3.11+
- Bot layer: `python-telegram-bot`
- Queue and persistence: SQLite + background worker thread
- Rendering: Pillow + FFmpeg

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
```

## Environment

Set these before running the bot:

```bash
export AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN="your_bot_token"
export AI_VIDEO_GEN_ALLOWED_CHAT_IDS="1702319284"
export AI_VIDEO_GEN_DEFAULT_CHAT_ID="1702319284"
```

For unattended startup, you can instead write them into `state/bot.env` or `.env`. The runner loads both automatically.

Optional:

```bash
export AI_VIDEO_GEN_TELEGRAM_PARSE_MODE="HTML"
export AI_VIDEO_GEN_SEND_RETRIES="3"
export AI_VIDEO_GEN_LOOP_BACKOFF_SECONDS="10"
export YOUTUBE_PRIVACY_STATUS="public"
export YOUTUBE_CATEGORY_ID="22"
export AI_VIDEO_GEN_YOUTUBE_RETRY_LIMIT="5"
```

YouTube uploads require OAuth credentials via environment variables:

```bash
export YOUTUBE_CLIENT_ID="..."
export YOUTUBE_CLIENT_SECRET="..."
export YOUTUBE_REFRESH_TOKEN="..."
```

## Requirements

- Python 3.11+
- FFmpeg available on `PATH`

Check FFmpeg:

```bash
ffmpeg -version
```

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## One-Command Install

Use the guided installer to collect your bot details, install dependencies, and configure autostart:

```bash
chmod +x installer.sh
./installer.sh
```

The installer:

- detects the Linux package manager and installs Python, FFmpeg, Node.js, npm, and fallback tools
- asks for bot token, chat IDs, and whether the bot should start on boot
- writes persistent config into `state/bot.env`
- chooses the best autostart method in this order: `systemd -> crontab -> screen via rc.local -> manual nohup`
- can start the bot immediately after setup

## Run The Bot

```bash
source .venv/bin/activate
python -m app.telegram_bot
```

Or via the wrapper:

```bash
./scripts/run_bot.sh
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
