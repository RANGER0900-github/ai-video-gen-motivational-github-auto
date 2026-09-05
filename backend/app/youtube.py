from __future__ import annotations

import asyncio
import json
import logging
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .config import AppConfig
from .models import JobDetail

logger = logging.getLogger(__name__)
DEFAULT_QUOTA_LIMIT = 10_000
DEFAULT_UPLOAD_COST = 1_600
UPLOAD_RETRY_COOLDOWN_SECONDS = 300
DESCRIPTION_VERSION = "v3"
TITLE_POOL = [
    "Discipline Builds You | AI Motivation #Shorts #motivation",
    "Stay Locked In | AI Motivation #Shorts #discipline",
    "No Excuses. Just Work. #Shorts #motivation",
    "Focus Like a Weapon | AI Motivation #Shorts",
    "Train Your Mind Daily #Shorts #selfdiscipline",
    "Winners Execute Daily #Shorts #motivation",
    "Built by Discipline | AI Motivation #Shorts",
    "Consistency Wins Long Term #Shorts #grindset",
    "You Become What You Repeat #Shorts #mindset",
    "Silence the Noise. Execute. #Shorts #focus",
    "Relentless Mindset Only #Shorts #motivation",
    "Average Never Wins #Shorts #successmindset",
]
DEFAULT_DESCRIPTION = """### Description (YouTube / Reels / Shorts)

Unlock your full potential with powerful AI-driven motivation. This video is built to push you beyond limits, cut through distractions, and reprogram your mindset for success. Whether you're grinding late nights, building your dream, staying disciplined in the gym, or forcing yourself to keep going when it's uncomfortable, this is your fuel.

Real progress comes from consistency, not emotion. Motivation starts the fire, but discipline keeps it burning. Train your mind to stay focused, ignore the noise, and execute daily. No excuses. No shortcuts. No waiting for the perfect moment.

This AI-generated motivation content blends cinematic visuals, intense pacing, and mindset-shifting energy to help you stay locked in. Use it when you're tired. Use it when you're distracted. Use it when you're tempted to quit. Rewire your habits. Sharpen your focus. Become unstoppable.

Success is not luck. It is discipline, clarity, sacrifice, and relentless execution stacked day after day.

If this hits, come back tomorrow and run it again.

---

### Hashtags

#motivation #motivationdaily #selfdiscipline #discipline #grindset #successmindset #winnermindset #stayhard #focus #consistency
#noexcuses #mindsetshift #workethic #disciplineequalsfreedom #mentalstrength #peakperformance #entrepreneur #hustle #dailygrind
#nevergiveup #goals #selfimprovement #productivity #riseandgrind #dreambig #successquotes #motivationalvideo #ai #aimotivation
#aivideo #cinematicvideo #viralvideo #shorts #reels #explorepage #fyp #trendingnow #gymmotivation #studyhard #lategrind
"""
DEFAULT_TAGS = [
    "motivation",
    "motivation daily",
    "self discipline",
    "discipline",
    "grindset",
    "success mindset",
    "winner mindset",
    "focus",
    "consistency",
    "no excuses",
    "mental strength",
    "self improvement",
    "ai motivation",
    "ai video",
    "cinematic motivation",
    "motivational video",
    "shorts",
]


class YouTubeQuotaExceeded(RuntimeError):
    pass


class YouTubeUploadError(RuntimeError):
    pass


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pick_title(job_id: int, items: list[dict[str, Any]], quote: str = "") -> str:
    quote_text = quote.strip().strip('"')
    if quote_text:
        for suffix in (" #Shorts", " #Shorts #motivation", " #Shorts #discipline"):
            limit = 100 - len(suffix)
            clipped = quote_text[:limit].rstrip(" .,!?:;\"'")
            if clipped:
                return f"{clipped}{suffix}"
    used = [item.get("title", "") for item in items if item.get("youtube_status") == "uploaded"]
    start = job_id % len(TITLE_POOL)
    for offset in range(len(TITLE_POOL)):
        candidate = TITLE_POOL[(start + offset) % len(TITLE_POOL)]
        if not used or candidate != used[-1]:
            return candidate
    return TITLE_POOL[start]


def build_description(quote: str = "", author: str = "") -> str:
    quote_text = quote.strip()
    author_text = author.strip()
    if not quote_text:
        return DEFAULT_DESCRIPTION
    intro_lines = [quote_text]
    if author_text:
        intro_lines.append(f"— {author_text}")
    intro_lines.append("")
    intro_lines.append("Save this short, share it, and come back when you need a reset.")
    intro_lines.append("")
    return "\n".join(intro_lines) + DEFAULT_DESCRIPTION


@dataclass(slots=True)
class UploadResult:
    video_id: str
    watch_url: str
    shorts_url: str
    title: str
    privacy: str


class YouTubeQueueStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self.path = config.youtube_queue_json
        self._lock = threading.Lock()

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "description_version": DESCRIPTION_VERSION,
            "quota": {
                "daily_limit_units": DEFAULT_QUOTA_LIMIT,
                "upload_cost_units": DEFAULT_UPLOAD_COST,
                "estimated_uploads_today": 0,
                "estimated_quota_units_used_today": 0,
                "quota_window_started_at": None,
                "quota_blocked_until_at": None,
                "quota_notice_sent_at": None,
                "last_quota_exhausted_at": None,
            },
            "items": [],
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            return self._refresh_quota_window(data, persist=True)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            data = self._default_state()
            self._write(data)
            return data
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
        temp_path.replace(self.path)

    def _refresh_quota_window(self, data: dict[str, Any], persist: bool) -> dict[str, Any]:
        changed = False
        quota = data.setdefault("quota", {})
        now = datetime.now(timezone.utc)
        window_started_at = quota.get("quota_window_started_at")
        if window_started_at is None:
            quota["quota_window_started_at"] = now.isoformat()
            changed = True
        else:
            started = datetime.fromisoformat(window_started_at)
            if (now - started).total_seconds() >= 86400:
                quota["quota_window_started_at"] = now.isoformat()
                quota["estimated_uploads_today"] = 0
                quota["estimated_quota_units_used_today"] = 0
                changed = True
        blocked_until_at = quota.get("quota_blocked_until_at")
        if blocked_until_at is not None:
            blocked_until = datetime.fromisoformat(blocked_until_at)
            if now >= blocked_until:
                quota["quota_blocked_until_at"] = None
                quota["quota_notice_sent_at"] = None
                changed = True
        if quota.get("quota_blocked_until_at") is None:
            for item in data.setdefault("items", []):
                if item.get("youtube_status") == "quota_blocked":
                    item["youtube_status"] = "pending"
                    item["last_error"] = "Retrying after 24h upload block expired"
        if changed and persist:
            self._write(data)
        return data

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.load())

    def status_summary(self) -> dict[str, Any]:
        data = self.load()
        items = data["items"]
        return {
            "pending": sum(1 for item in items if item.get("youtube_status") in {"pending", "uploading", "quota_blocked"}),
            "uploaded": sum(1 for item in items if item.get("youtube_status") == "uploaded"),
            "failed": sum(1 for item in items if item.get("youtube_status") == "failed"),
            "quota_blocked": bool(data["quota"].get("quota_blocked_until_at")),
            "quota_blocked_until_at": data["quota"].get("quota_blocked_until_at"),
            "estimated_uploads_today": int(data["quota"].get("estimated_uploads_today", 0)),
            "estimated_quota_units_used_today": int(data["quota"].get("estimated_quota_units_used_today", 0)),
        }

    def enqueue_job(self, job: JobDetail, *, youtube_enabled_for_origin: bool) -> dict[str, Any]:
        with self._lock:
            data = self._refresh_quota_window(self._load_unlocked(), persist=False)
            items = data["items"]
            existing = next((item for item in items if item.get("job_id") == job.id), None)
            if existing:
                existing["output_path"] = job.output_path
                existing["current_filename"] = job.output_path
                existing["chat_id"] = job.chat_id
                if existing.get("youtube_status") == "failed":
                    existing["youtube_status"] = "pending"
                    existing["last_error"] = None
                self._write(data)
                return deepcopy(existing)
            item = {
                "job_id": job.id,
                "chat_id": job.chat_id,
                "output_path": job.output_path,
                "current_filename": job.output_path,
                "created_at": job.created_at.isoformat(),
                "telegram_sent_at": job.delivered_at.isoformat() if job.delivered_at else utcnow_iso(),
                "telegram_message_id": job.telegram_message_id,
                "youtube_enabled_for_origin": youtube_enabled_for_origin,
                "youtube_status": "pending",
                "youtube_video_id": None,
                "youtube_url": None,
                "youtube_shorts_url": None,
                "quote": job.quote,
                "author": job.author,
                "title": pick_title(job.id, items, job.quote),
                "description_version": DESCRIPTION_VERSION,
                "tags": list(DEFAULT_TAGS),
                "privacy_status": self.config.youtube_privacy_status,
                "category_id": self.config.youtube_category_id,
                "attempt_count": 0,
                "last_attempt_at": None,
                "last_error": None,
                "quota_cost": DEFAULT_UPLOAD_COST,
                "renamed_yt_done": False,
                "retry_disabled": False,
            }
            items.append(item)
            self._write(data)
            return deepcopy(item)

    def enqueue_loop_job(self, job: JobDetail) -> dict[str, Any]:
        return self.enqueue_job(job, youtube_enabled_for_origin=True)

    def get_item(self, job_id: int) -> dict[str, Any] | None:
        data = self.load()
        for item in data["items"]:
            if int(item.get("job_id")) == int(job_id):
                return deepcopy(item)
        return None

    def next_ready_item(self) -> dict[str, Any] | None:
        data = self.load()
        blocked_until_at = data["quota"].get("quota_blocked_until_at")
        if blocked_until_at:
            return None
        now = datetime.now(timezone.utc)
        for item in data["items"]:
            status = str(item.get("youtube_status") or "")
            if status not in {"pending", "failed"}:
                continue
            if bool(item.get("retry_disabled")):
                continue
            if status == "failed":
                last_attempt = parse_utc_iso(str(item.get("last_attempt_at") or ""))
                if last_attempt is not None and (now - last_attempt).total_seconds() < UPLOAD_RETRY_COOLDOWN_SECONDS:
                    continue
            if bool(item.get("youtube_enabled_for_origin")) or int(item.get("attempt_count", 0)) < self.config.youtube_retry_limit:
                return deepcopy(item)
        return None

    def disable_retry(self, job_id: int, error: str) -> dict[str, Any]:
        with self._lock:
            data = self._refresh_quota_window(self._load_unlocked(), persist=False)
            item = self._item_by_job_id(data, job_id)
            item["youtube_status"] = "failed"
            item["retry_disabled"] = True
            item["youtube_enabled_for_origin"] = False
            item["last_error"] = error[:500]
            self._write(data)
            return deepcopy(item)

    def mark_uploading(self, job_id: int) -> dict[str, Any]:
        with self._lock:
            data = self._refresh_quota_window(self._load_unlocked(), persist=False)
            item = self._item_by_job_id(data, job_id)
            item["youtube_status"] = "uploading"
            item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
            item["last_attempt_at"] = utcnow_iso()
            self._write(data)
            return deepcopy(item)

    def mark_uploaded(self, job_id: int, *, result: UploadResult, new_output_path: str, renamed_yt_done: bool) -> dict[str, Any]:
        with self._lock:
            data = self._refresh_quota_window(self._load_unlocked(), persist=False)
            item = self._item_by_job_id(data, job_id)
            item["youtube_status"] = "uploaded"
            item["youtube_video_id"] = result.video_id
            item["youtube_url"] = result.watch_url
            item["youtube_shorts_url"] = result.shorts_url
            item["current_filename"] = new_output_path
            item["output_path"] = new_output_path
            item["renamed_yt_done"] = renamed_yt_done
            item["last_error"] = None
            quota = data["quota"]
            quota["estimated_uploads_today"] = int(quota.get("estimated_uploads_today", 0)) + 1
            quota["estimated_quota_units_used_today"] = int(quota.get("estimated_quota_units_used_today", 0)) + DEFAULT_UPLOAD_COST
            self._write(data)
            return deepcopy(item)

    def mark_failed(self, job_id: int, error: str, *, quota_exceeded: bool = False) -> dict[str, Any]:
        with self._lock:
            data = self._refresh_quota_window(self._load_unlocked(), persist=False)
            item = self._item_by_job_id(data, job_id)
            item["youtube_status"] = "quota_blocked" if quota_exceeded else "failed"
            item["last_error"] = error[:500]
            if quota_exceeded:
                exhausted_at = datetime.now(timezone.utc)
                data["quota"]["quota_blocked_until_at"] = (exhausted_at + timedelta(hours=24)).isoformat()
                data["quota"]["last_quota_exhausted_at"] = exhausted_at.isoformat()
                if not data["quota"].get("quota_notice_sent_at"):
                    data["quota"]["quota_notice_sent_at"] = exhausted_at.isoformat()
            self._write(data)
            return deepcopy(item)

    def _item_by_job_id(self, data: dict[str, Any], job_id: int) -> dict[str, Any]:
        for item in data["items"]:
            if int(item.get("job_id")) == int(job_id):
                return item
        raise KeyError(job_id)


async def upload_with_node(config: AppConfig, *, video_path: Path, title: str, description: str, tags: list[str], privacy_status: str, category_id: str) -> UploadResult:
    args = [
        "node",
        str(config.upload_js_path),
        "--json",
        "--file",
        str(video_path),
        "--title",
        title,
        "--privacy",
        privacy_status,
        "--description",
        description,
        "--tags",
        ",".join(tags),
        "--category",
        category_id,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(config.root_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()
    error_output = stderr.decode("utf-8", errors="replace").strip()
    payload: dict[str, Any] | None = None
    if output:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = None
    if process.returncode != 0:
        message = (payload or {}).get("message") or error_output or output or f"node upload failed with code {process.returncode}"
        reason = (payload or {}).get("reason", "")
        lower_message = message.lower()
        lower_reason = reason.lower()
        if (
            "quota" in lower_message
            or "quota" in lower_reason
            or "exceeded the number of videos they may upload" in lower_message
            or "exceeded the number of videos they may upload" in lower_reason
        ):
            raise YouTubeQuotaExceeded(message)
        raise YouTubeUploadError(message)
    if not payload or not payload.get("ok"):
        raise YouTubeUploadError(error_output or output or "Upload response was not valid JSON")
    return UploadResult(
        video_id=payload["videoId"],
        watch_url=payload["watchUrl"],
        shorts_url=payload["shortsUrl"],
        title=payload.get("title", title),
        privacy=payload.get("privacy", privacy_status),
    )


def rename_uploaded_file(root_dir: Path, relative_path: str) -> tuple[str, bool]:
    current = root_dir / relative_path
    if not current.exists():
        raise FileNotFoundError(f"Cannot rename missing video: {current}")
    if current.stem.endswith("_yt-done"):
        return relative_path, False
    renamed = current.with_name(f"{current.stem}_yt-done{current.suffix}")
    current.replace(renamed)
    return renamed.relative_to(root_dir).as_posix(), True
