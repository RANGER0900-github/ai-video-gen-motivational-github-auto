from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    root_dir: Path
    state_dir: Path
    db_path: Path
    images_dir: Path
    videos_dir: Path
    music_dir: Path
    fonts_dir: Path
    outputs_dir: Path
    watermark_path: Path
    quotes_csv: Path
    images_usage_json: Path
    media_usage_json: Path
    youtube_queue_json: Path
    instagram_queue_json: Path
    upload_js_path: Path
    instagram_upload_script: Path
    instagram_cookies_path: Path
    instagram_storage_path: Path
    instagram_profile_dir: Path | None
    instagram_profile_name: str
    instagram_target_username: str
    instagram_upload_timeout_seconds: int
    voice_model: str = "en-US-BrianMultilingualNeural"
    voice_pitch: str = "-6Hz"
    voice_rate: str = "-5%"
    voice_volume: float = 1.0
    music_volume: float = 0.10
    video_sfx_volume: float = 0.15
    whisper_model_size: str = "base.en"
    max_duration: float = 20.0
    fps: int = 24
    width: int = 1080
    height: int = 1920
    text_fade: float = 0.5
    crf: str = "20"
    encoder_preset: str = "veryfast"
    encoder_threads: int = 4
    default_darken: float = 0.78
    default_workers: int = 1
    telegram_bot_token: str | None = None
    allowed_chat_ids: tuple[int, ...] = ()
    default_chat_id: int | None = None
    telegram_parse_mode: str = "HTML"
    send_retries: int = 3
    loop_backoff_seconds: int = 600
    youtube_privacy_status: str = "public"
    youtube_category_id: str = "22"
    youtube_retry_limit: int = 5
    instagram_retry_limit: int = 3

    @property
    def process_log(self) -> Path:
        return self.outputs_dir / "process.log"


def load_config(root_dir: Path | None = None) -> AppConfig:
    root = Path(root_dir or os.getenv("AI_VIDEO_GEN_ROOT") or Path(__file__).resolve().parents[2]).resolve()
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir = root / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    allowed_chat_ids_raw = os.getenv("AI_VIDEO_GEN_ALLOWED_CHAT_IDS", "")
    allowed_chat_ids = tuple(
        int(part.strip())
        for part in allowed_chat_ids_raw.split(",")
        if part.strip()
    )
    default_chat_id_raw = os.getenv("AI_VIDEO_GEN_DEFAULT_CHAT_ID", "").strip()

    return AppConfig(
        root_dir=root,
        state_dir=state_dir,
        db_path=state_dir / "app.db",
        images_dir=root / "images",
        videos_dir=root / "videos",
        music_dir=root / "music",
        fonts_dir=root / "fonts",
        outputs_dir=root / "outputs",
        watermark_path=root / "assets" / "watermark.png",
        quotes_csv=root / "quotes.csv",
        images_usage_json=root / "images_usage.json",
        media_usage_json=root / "media_usage.json",
        youtube_queue_json=state_dir / "youtube_queue.json",
        instagram_queue_json=state_dir / "instagram_queue.json",
        upload_js_path=root / "upload.js",
        instagram_upload_script=root / "scripts" / "ig_upload_playwright.py",
        instagram_cookies_path=Path(os.getenv("AI_VIDEO_GEN_INSTAGRAM_COOKIES_PATH", "/home/meet/Downloads/cookies (2).txt")).resolve(),
        instagram_storage_path=Path(os.getenv("AI_VIDEO_GEN_INSTAGRAM_STORAGE_PATH", str(state_dir / "ig_storage.json"))).resolve(),
        instagram_profile_dir=Path(profile_dir).resolve() if (profile_dir := os.getenv("AI_VIDEO_GEN_INSTAGRAM_PROFILE_DIR", "").strip()) else None,
        instagram_profile_name=os.getenv("AI_VIDEO_GEN_INSTAGRAM_PROFILE_NAME", "Default").strip() or "Default",
        instagram_target_username=os.getenv("AI_VIDEO_GEN_INSTAGRAM_TARGET_USERNAME", "void.to.victory").strip() or "void.to.victory",
        instagram_upload_timeout_seconds=int(os.getenv("AI_VIDEO_GEN_INSTAGRAM_UPLOAD_TIMEOUT_SECONDS", "420")),
        telegram_bot_token=os.getenv("AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=allowed_chat_ids,
        default_chat_id=int(default_chat_id_raw) if default_chat_id_raw else (allowed_chat_ids[0] if allowed_chat_ids else None),
        telegram_parse_mode=os.getenv("AI_VIDEO_GEN_TELEGRAM_PARSE_MODE", "HTML"),
        send_retries=int(os.getenv("AI_VIDEO_GEN_SEND_RETRIES", "3")),
        loop_backoff_seconds=int(os.getenv("AI_VIDEO_GEN_LOOP_BACKOFF_SECONDS", "600")),
        youtube_privacy_status=os.getenv("YOUTUBE_PRIVACY_STATUS", "public"),
        youtube_category_id=os.getenv("YOUTUBE_CATEGORY_ID", "22"),
        youtube_retry_limit=int(os.getenv("AI_VIDEO_GEN_YOUTUBE_RETRY_LIMIT", "5")),
        instagram_retry_limit=int(os.getenv("AI_VIDEO_GEN_INSTAGRAM_RETRY_LIMIT", "3")),
    )



def check_runtime(config: AppConfig) -> list[str]:
    issues: list[str] = []
    for directory in (config.images_dir, config.videos_dir, config.music_dir, config.fonts_dir, config.outputs_dir, config.state_dir):
        if not directory.exists():
            issues.append(f"Missing directory: {directory}")
    if not config.quotes_csv.exists():
        issues.append(f"Missing quotes CSV: {config.quotes_csv}")
    if shutil.which("ffmpeg") is None:
        issues.append("ffmpeg is not available on PATH")
    return issues
