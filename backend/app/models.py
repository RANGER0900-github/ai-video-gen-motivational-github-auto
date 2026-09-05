from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "preparing", "rendering", "finalizing", "completed", "failed", "cancelled"]
JobOrigin = Literal["manual", "loop", "resend"]
DeliveryStatus = Literal["pending", "sending", "sent", "failed", "skipped"]


class QuoteRecord(BaseModel):
    row_id: int
    quote: str
    author: str = ""
    status: str = ""
    used_time: str = ""
    output: str = ""
    error: str = ""


class CreateJobRequest(BaseModel):
    row_ids: list[int] = Field(default_factory=list)
    custom_quote: str | None = None
    custom_author: str | None = None
    image_name: str | None = None
    music_name: str | None = None
    darken: float | None = None


class JobSummary(BaseModel):
    id: int
    status: JobStatus
    progress: float
    phase: str
    message: str
    quote: str
    author: str | None = None
    image_name: str | None = None
    music_name: str | None = None
    output_path: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    origin: JobOrigin = "manual"
    chat_id: int | None = None
    batch_id: int | None = None
    delivery_status: DeliveryStatus = "pending"
    delivery_message: str | None = None
    delivered_at: datetime | None = None
    telegram_file_id: str | None = None
    telegram_message_id: int | None = None


class JobDetail(JobSummary):
    source_row_id: int | None = None
    darken: float


class ProgressEvent(BaseModel):
    id: int
    job_id: int
    status: JobStatus
    phase: str
    progress: float
    message: str
    created_at: datetime


class AssetItem(BaseModel):
    name: str
    path: str
    url: str


class VideoItem(BaseModel):
    job_id: int | None = None
    name: str
    path: str
    url: str
    created_at: str | None = None
    title: str | None = None
    quote: str | None = None
    author: str | None = None


class BotState(BaseModel):
    id: int = 1
    loop_enabled: bool = False
    loop_chat_id: int | None = None
    loop_youtube_enabled: bool = False
    loop_instagram_enabled: bool = False
    loop_telegram_enabled: bool = True
    loop_interval_seconds: int = 600
    loop_started_at: datetime | None = None
    stop_requested: bool = False
    last_startup_at: datetime | None = None


class JobBatch(BaseModel):
    id: int
    chat_id: int
    kind: Literal["manual", "resend"]
    requested_count: int
    completed_count: int = 0
    failed_count: int = 0
    status: Literal["queued", "active", "completed", "failed", "cancelled"] = "queued"
    progress_message_id: int | None = None
    created_at: datetime
    updated_at: datetime
