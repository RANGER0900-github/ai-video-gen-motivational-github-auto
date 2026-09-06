# AI Motivational Video Generator (GitHub Actions Auto-Upload)

An automated, serverless motivational video generator and YouTube Shorts publishing pipeline powered by GitHub Actions.

## How It Works

1. **Scheduled Trigger**: GitHub Actions runs on an hourly cron schedule 24 times a day around the clock (`0 * * * *`). It can also be triggered on-demand via `workflow_dispatch` in the GitHub Actions UI.
2. **Guaranteed 7:3 Media Ratio Scheduler**: For every 10 runs, exactly **7 runs produce dynamic video backgrounds** from `videos/` and **3 runs produce image backgrounds** from `images/` (yielding ~17 videos and ~7 images per day). An organic 10-run shuffled deck ensures the sequence is randomized, and assets rotate through least-used items so footage and images cycle fairly without repetition.
3. **Brian Neural Voice & Whisper Subtitles**: Quotes are voiced using Edge-TTS Brian (`en-US-BrianMultilingualNeural`, `-6Hz` pitch, `-5%` rate). Subtitles are synced with word-level timestamps extracted via Faster-Whisper, alternating between Spotlight and Cumulative Gold Wave animated karaoke effects.
4. **Watermark Branding & Logo Concealment**: Every video features an antialiased channel watermark overlay (`assets/watermark.png`) at `x=835, y=1675`, seamlessly covering the bottom-right Gemini star logo.
5. **Acoustic Audio Mixing**: Automatically balances 3 audio tracks: 100% voiceover clarity, 10% (*-20 dB*) *"Me and the Devil"* soundtrack, and 15% video ambient sound effects, with a smooth 1-second outro audio fade.
6. **YouTube Shorts Upload**: Node.js (`upload.js`) uploads the generated video directly to YouTube Shorts with dynamic title generation derived from the quote, rich descriptions, and trending motivational hashtags.
7. **State Persistence**: GitHub Actions automatically commits and pushes updated `quotes.csv`, `media_usage.json`, and `images_usage.json` back to the repository with `[skip ci]`.
8. **Zero Repo Bloat**: Temporary video outputs in `outputs/` are discarded after each run, keeping the git repository clean.

## Quota & Scheduling

The workflow schedule in `.github/workflows/auto_upload.yml` runs every hour (24 times a day):
```yaml
schedule:
  # Runs hourly: 24 times a day around the clock
  - cron: '0 * * * *'
```

## GitHub Secrets

Add these three secrets under **Settings > Secrets and variables > Actions**:

| Secret Name | Description |
|---|---|
| `YOUTUBE_CLIENT_ID` | OAuth 2.0 Client ID from Google Cloud Console |
| `YOUTUBE_CLIENT_SECRET` | OAuth 2.0 Client Secret from Google Cloud Console |
| `YOUTUBE_REFRESH_TOKEN` | OAuth 2.0 Refresh Token with `youtube.upload` scope |

### Generating Your Refresh Token
Run the included token helper:
```bash
node scripts/get_youtube_token.js
```
Follow the URL prompt to authorize and paste back the authorization code.

## Project Structure

```text
.github/workflows/
  auto_upload.yml       # GitHub Actions scheduled workflow
assets/
  watermark.png         # Channel watermark badge overlay (covers Gemini star)
backend/
  app/
    cli.py              # CLI entry point (--media-type auto|video|image)
    config.py           # App configuration, audio mixing, and paths
    csv_store.py        # quotes.csv reader, writer, and selection logic
    database.py         # SQLite job and event database
    jobs.py             # Render queue and worker service
    models.py           # Pydantic data models
    renderer.py         # Brian TTS + Whisper + ASS + FFmpeg video generation engine
    storage.py          # 5:1 media scheduler and asset rotation
videos/                 # Cinematic portrait background videos
images/                 # Portrait background images
music/                  # Background audio tracks (Me and the Devil)
fonts/                  # Font files (NotoSans, PlayfairDisplay)
quotes.csv              # Quote library (auto-updated with usage timestamps)
media_usage.json        # 5:1 scheduler counter and media rotation state
upload.js               # Node.js YouTube OAuth2 uploader
scripts/
  get_youtube_token.js  # Helper to generate YouTube OAuth refresh token
```

## Local Testing

### 1. Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
npm install
```

### 2. Generate a Test Video
```bash
python -m app.cli --count 1
```
The rendered video will appear in `outputs/`.

### 3. Test YouTube Upload
Set your credentials in `.env` (gitignored) or shell:
```bash
export YOUTUBE_CLIENT_ID="..."
export YOUTUBE_CLIENT_SECRET="..."
export YOUTUBE_REFRESH_TOKEN="..."

node upload.js
```
