# AI Motivational Video Generator (GitHub Actions Auto-Upload)

An automated, serverless motivational video generator and YouTube Shorts publishing pipeline powered by GitHub Actions.

## How It Works

1. **Scheduled Trigger**: GitHub Actions runs on a cron schedule 6 times a day (every 4 hours around the clock at 00:00, 04:00, 08:00, 12:00, 16:00, and 20:00 UTC) to match the free YouTube Data API quota limit. It can also be triggered on-demand via `workflow_dispatch` in the GitHub Actions UI.
2. **Video Rendering**: Python selects an unused quote from `quotes.csv` and the least-used background image from `images_usage.json`. Pillow renders smooth, wrapped, faded text overlays, and FFmpeg pairs them with background music into a crisp 1080x1920 (9:16 portrait) video.
3. **YouTube Shorts Upload**: Node.js (`upload.js`) uploads the generated video directly to YouTube with an auto-generated title derived from the quote, rich descriptions, and trending motivational hashtags.
4. **State Persistence**: GitHub Actions automatically commits and pushes the updated `quotes.csv` (with the quote marked as `used`) and `images_usage.json` (with updated image counters) back to the repository with `[skip ci]`.
5. **No Bloat**: Temporary video files in `outputs/` are discarded when the runner stops, keeping your git repository lean and fast.

## Quota & Scheduling

The free tier of the YouTube Data API provides **10,000 units per day**. Each video upload costs **1,600 units**, allowing a maximum of **6 video uploads per day** ($10,000 / 1,600 = 6$).

The workflow schedule in `.github/workflows/auto_upload.yml`:
```yaml
schedule:
  # Runs 6 times a day: every 4 hours (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC)
  - cron: '0 0,4,8,12,16,20 * * *'
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
