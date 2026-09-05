from __future__ import annotations

import atexit
import asyncio
import errno
import html
import logging
import os
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fcntl
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import AppConfig, load_config
from .csv_store import QuoteStore
from .database import Database, row_to_batch, row_to_bot_state, row_to_job
from .jobs import JobContext, JobService
from .models import CreateJobRequest, JobBatch, JobDetail
from .storage import AssetStore
from .instagram import (
    InstagramQueueStore,
    InstagramUploadError,
    upload_to_instagram,
)
from .youtube import (
    DEFAULT_DESCRIPTION,
    DEFAULT_TAGS,
    DESCRIPTION_VERSION,
    YouTubeQueueStore,
    YouTubeQuotaExceeded,
    YouTubeUploadError,
    build_description,
    pick_title,
    rename_uploaded_file,
    upload_with_node,
)

logger = logging.getLogger(__name__)
_INSTANCE_LOCK_HANDLE = None

PAGE_SIZE = 5
COUNT_PRESETS = (1, 3, 5, 10)
LOOP_INTERVAL_PRESETS = (600, 1200, 1800, 3600)
ACTIVE_STATUSES = {"queued", "preparing", "rendering", "finalizing"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ascii_bar(progress: float, width: int = 18) -> str:
    progress = max(0.0, min(1.0, progress))
    filled = min(width, int(round(progress * width)))
    return f"[{'=' * filled}{'>' if filled < width else ''}{'.' * max(0, width - filled - (1 if filled < width else 0))}]"


class TelegramBotRuntime:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database(config.db_path)
        self.quotes = QuoteStore(config.quotes_csv)
        self.assets = AssetStore(config)
        self.job_service = JobService(JobContext(config=config, db=self.db, assets=self.assets, quotes=self.quotes))
        self._background_task: asyncio.Task | None = None
        self._batch_text_cache: dict[int, str] = {}
        self._chat_action_tasks: dict[int, tuple[str, asyncio.Task]] = {}
        self.youtube_queue = YouTubeQueueStore(config)
        self.instagram_queue = InstagramQueueStore(config)
        self._youtube_upload_task: asyncio.Task | None = None
        self._instagram_upload_task: asyncio.Task | None = None
        self._instagram_upload_job_id: int | None = None
        self._instagram_upload_started_at: datetime | None = None

    async def post_init(self, application: Application) -> None:
        if not self.config.telegram_bot_token:
            raise RuntimeError("AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN is not configured")
        if not self.config.allowed_chat_ids:
            raise RuntimeError("AI_VIDEO_GEN_ALLOWED_CHAT_IDS is not configured")
        self.quotes.normalize()
        self.job_service.start()
        self.instagram_queue.recover_stale_state()
        self.db.update_bot_state(last_startup_at=utcnow_iso())
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_my_commands(
            commands=[
                BotCommand("start", "Open the control panel"),
                BotCommand("generate_video", "Generate one or more new videos"),
                BotCommand("video_loop", "Start infinite one-by-one generation"),
                BotCommand("list", "List completed videos"),
                BotCommand("status", "Show queue and loop status"),
                BotCommand("stop", "Stop loop mode and cancel loop work"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        self._background_task = asyncio.create_task(self._background_loop(application), name="telegram-bot-background")

    async def post_shutdown(self, application: Application) -> None:
        task = self._background_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._youtube_upload_task is not None:
            self._youtube_upload_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._youtube_upload_task
        if self._instagram_upload_task is not None:
            self._instagram_upload_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._instagram_upload_task
        for action, action_task in self._chat_action_tasks.values():
            action_task.cancel()
        for action, action_task in self._chat_action_tasks.values():
            with suppress(asyncio.CancelledError):
                await action_task
        self._chat_action_tasks.clear()
        self.job_service.stop()

    def completed_jobs(self) -> list[JobDetail]:
        jobs = [self.job_service.get_job(job.id) for job in self.job_service.list_jobs() if job.status == "completed" and job.output_path]
        playable: list[JobDetail] = []
        for job in jobs:
            if job.output_path and (self.config.root_dir / job.output_path).exists():
                playable.append(job)
        return sorted(playable, key=lambda item: item.completed_at or item.updated_at, reverse=True)

    def bot_state(self):
        return row_to_bot_state(self.db.get_bot_state_row())

    def open_batches(self) -> list[JobBatch]:
        return [row_to_batch(row) for row in self.db.list_open_batches()]

    def is_allowed_chat(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self.config.allowed_chat_ids

    def active_jobs(self) -> list[JobDetail]:
        return [self.job_service.get_job(job.id) for job in self.job_service.list_jobs() if job.status in ACTIVE_STATUSES]

    def active_loop_jobs(self) -> list[JobDetail]:
        return [job for job in self.active_jobs() if job.origin == "loop"]

    def recent_loop_job(self) -> JobDetail | None:
        for job in [self.job_service.get_job(item.id) for item in self.job_service.list_jobs()]:
            if job.origin == "loop":
                return job
        return None

    def status_text(self) -> str:
        jobs = self.job_service.list_jobs()
        active = [job for job in jobs if job.status in ACTIVE_STATUSES]
        completed = [job for job in jobs if job.status == "completed"]
        failed = [job for job in jobs if job.status == "failed"]
        state = self.bot_state()
        yt = self.youtube_queue.status_summary()
        ig = self.instagram_queue.status_summary()
        platforms = self._loop_platforms_text(state)
        lines = [
            "📊 <b>Generator Status</b>",
            f"🔁 Loop: <b>{'ON' if state.loop_enabled else 'OFF'}</b>",
            f"⏱️ Interval: <b>{self._format_interval(state.loop_interval_seconds)}</b>",
            f"🌐 Platforms: <b>{platforms}</b>",
            f"🎬 In progress: <b>{len(active)}</b>",
            f"✅ Completed: <b>{len(completed)}</b>",
            f"❌ Failed: <b>{len(failed)}</b>",
            f"📺 YT pending: <b>{yt['pending']}</b> · Uploaded today: <b>{yt['estimated_uploads_today']}</b>",
            f"📸 IG pending: <b>{ig['pending']}</b> · Uploaded: <b>{ig['uploaded']}</b>",
        ]
        if yt["quota_blocked"]:
            until = yt["quota_blocked_until_at"] or ""
            lines.append(f"⏳ YouTube blocked until <b>{html.escape(until)}</b>")
        if active:
            current = active[0]
            lines.extend(
                [
                    "",
                    f"Now running: <b>Job #{current.id}</b>",
                    f"{html.escape(current.phase)} · {current.progress * 100:.0f}%",
                    html.escape(current.message),
                ]
            )
        elif completed:
            last = completed[0]
            lines.extend(
                [
                    "",
                    f"Last done: <b>Job #{last.id}</b>",
                    html.escape((last.quote[:90] + "...") if len(last.quote) > 90 else last.quote),
                ]
            )
        return "\n".join(lines)

    def _format_interval(self, seconds: int) -> str:
        minutes = max(1, seconds // 60)
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours} hour{'s' if hours != 1 else ''}"
        return f"{minutes} min"

    def _loop_platforms_text(self, state) -> str:
        platforms: list[str] = []
        if state.loop_telegram_enabled:
            platforms.append("Telegram")
        if state.loop_youtube_enabled:
            platforms.append("YouTube")
        if state.loop_instagram_enabled:
            platforms.append("Instagram")
        return ", ".join(platforms) if platforms else "None"

    def _instagram_stale_after_seconds(self) -> int:
        configured_timeout = max(60, int(self.config.instagram_upload_timeout_seconds))
        return configured_timeout + 180

    async def _send_logged_message(self, bot, reason: str, **kwargs):
        logger.info(
            "Sending Telegram text message: reason=%s chat_id=%s reply_to=%s",
            reason,
            kwargs.get("chat_id"),
            kwargs.get("reply_to_message_id"),
        )
        return await bot.send_message(**kwargs)

    async def _background_loop(self, application: Application) -> None:
        while True:
            try:
                await self._tick(application)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background loop tick failed")
            await asyncio.sleep(3)

    async def _tick(self, application: Application) -> None:
        await self._maintain_loop(application)
        await self._deliver_completed_jobs(application)
        await self._maybe_process_youtube_queue(application)
        await self._maybe_process_instagram_queue(application)
        await self._refresh_batches(application)
        await self._sync_chat_actions(application)
        state = self.bot_state()
        if state.stop_requested and not self.active_loop_jobs():
            self.db.update_bot_state(stop_requested=False)

    async def _maybe_process_youtube_queue(self, application: Application) -> None:
        if self._youtube_upload_task is not None and not self._youtube_upload_task.done():
            return
        item = self.youtube_queue.next_ready_item()
        if item is None:
            return
        self._youtube_upload_task = asyncio.create_task(
            self._upload_queued_video(application, item["job_id"]),
            name=f"youtube-upload-{item['job_id']}",
        )

    async def _maybe_process_instagram_queue(self, application: Application) -> None:
        stale_after_seconds = self._instagram_stale_after_seconds()
        stale_jobs = self.instagram_queue.recover_stalled_uploading(stale_after_seconds)
        for stale_job_id in stale_jobs:
            logger.warning("Recovered stale Instagram queue item: job_id=%s", stale_job_id)
        if self._instagram_upload_task is not None and not self._instagram_upload_task.done():
            if self._instagram_upload_started_at is not None:
                age_seconds = (datetime.now(timezone.utc) - self._instagram_upload_started_at).total_seconds()
                if age_seconds > stale_after_seconds:
                    logger.error(
                        "Canceling stale Instagram upload task: job_id=%s age_seconds=%s",
                        self._instagram_upload_job_id,
                        int(age_seconds),
                    )
                    self._instagram_upload_task.cancel()
            return
        item = self.instagram_queue.next_ready_item()
        if item is None:
            return
        self._instagram_upload_job_id = int(item["job_id"])
        self._instagram_upload_started_at = datetime.now(timezone.utc)
        self._instagram_upload_task = asyncio.create_task(
            self._upload_queued_instagram(application, item["job_id"]),
            name=f"instagram-upload-{item['job_id']}",
        )

    async def _upload_queued_video(self, application: Application, job_id: int) -> None:
        try:
            item = self.youtube_queue.mark_uploading(job_id)
            job = self.job_service.get_job(job_id)
            if not job.output_path:
                raise FileNotFoundError(f"Job #{job_id} has no output_path")
            output_path = self.config.root_dir / job.output_path
            if not output_path.exists():
                raise FileNotFoundError(f"Output file missing for job #{job_id}: {output_path}")
            result = await upload_with_node(
                self.config,
                video_path=output_path,
                title=item["title"],
                description=build_description(str(item.get("quote") or ""), str(item.get("author") or "")),
                tags=list(DEFAULT_TAGS),
                privacy_status=item.get("privacy_status", self.config.youtube_privacy_status),
                category_id=item.get("category_id", self.config.youtube_category_id),
            )
            new_output_path, renamed = rename_uploaded_file(self.config.root_dir, job.output_path)
            self.db.update_job_output_path(job.id, new_output_path)
            queue_item = self.youtube_queue.mark_uploaded(
                job.id,
                result=result,
                new_output_path=new_output_path,
                renamed_yt_done=renamed,
            )
            if job.chat_id is not None and not self._should_suppress_loop_platform_success(job):
                await application.bot.send_message(
                    chat_id=job.chat_id,
                    text=self._youtube_success_text(queue_item),
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=job.telegram_message_id,
                )
        except YouTubeQuotaExceeded as exc:
            before = self.youtube_queue.get_item(job_id)
            item = self.youtube_queue.mark_failed(job_id, str(exc), quota_exceeded=True)
            chat_id = item.get("chat_id")
            if chat_id and (before or {}).get("youtube_status") != "quota_blocked":
                summary = self.youtube_queue.status_summary()
                await self._send_logged_message(
                    application.bot,
                    "youtube_quota_blocked",
                    chat_id=chat_id,
                    text=(
                        "⚠️ <b>YouTube uploads are blocked for 24 hours</b>\n"
                        f"Saved for later: <b>{summary['pending']}</b> pending upload(s)."
                    ),
                    parse_mode=ParseMode.HTML,
                )
        except FileNotFoundError as exc:
            self.youtube_queue.disable_retry(job_id, str(exc))
            logger.warning("Disabled YouTube retry for missing output: job_id=%s error=%s", job_id, exc)
        except Exception as exc:
            item = self.youtube_queue.mark_failed(job_id, str(exc), quota_exceeded=False)
            chat_id = item.get("chat_id")
            if chat_id and not self._should_suppress_loop_retry_notice(job):
                await self._send_logged_message(
                    application.bot,
                    "youtube_upload_failed",
                    chat_id=chat_id,
                    text=(
                        "⚠️ <b>YouTube upload failed</b>\n"
                        f"Job #{job_id} is still queued for retry.\n"
                        f"<code>{html.escape(str(exc)[:300])}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
        finally:
            self._youtube_upload_task = None

    async def _upload_queued_instagram(self, application: Application, job_id: int) -> None:
        was_blocked = self.instagram_queue.status_summary().get("blocked", False)
        logger.info("Starting Instagram upload worker for job_id=%s", job_id)
        try:
            item = self.instagram_queue.mark_uploading(job_id)
            job = self.job_service.get_job(job_id)
            if not job.output_path:
                raise FileNotFoundError(f"Job #{job_id} has no output_path")
            output_path = self.config.root_dir / job.output_path
            if not output_path.exists():
                raise FileNotFoundError(f"Output file missing for job #{job_id}: {output_path}")
            if job.chat_id is not None and self._should_send_instagram_started_notice(item):
                await self._send_logged_message(
                    application.bot,
                    "instagram_upload_started",
                    chat_id=job.chat_id,
                    text=(
                        "📸 <b>Instagram upload started</b>\n"
                        "This can take a few minutes on the VPS. I will send the final Instagram URL after publish."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=job.telegram_message_id,
                )
            result = await upload_to_instagram(
                self.config,
                video_path=output_path,
                caption=str(item.get("caption") or ""),
            )
            queue_item = self.instagram_queue.mark_uploaded(job.id, result=result)
            if job.chat_id is not None and not self._should_suppress_loop_platform_success(job):
                await application.bot.send_message(
                    chat_id=job.chat_id,
                    text=self._instagram_success_text(queue_item),
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=job.telegram_message_id,
                )
            logger.info("Completed Instagram upload worker for job_id=%s", job_id)
        except asyncio.CancelledError:
            logger.warning("Instagram upload worker canceled for job_id=%s", job_id)
            try:
                self.instagram_queue.mark_failed(job_id, "Instagram upload worker canceled by watchdog.", blocked_reason=None)
            except Exception:
                logger.exception("Failed to mark Instagram queue item canceled for job_id=%s", job_id)
            raise
        except Exception as exc:
            blocked_reason = self._instagram_block_reason(str(exc))
            item = self.instagram_queue.mark_failed(job_id, str(exc), blocked_reason=blocked_reason)
            logger.exception("Instagram upload worker failed for job_id=%s", job_id)
            chat_id = item.get("chat_id")
            if chat_id:
                if blocked_reason:
                    if not was_blocked:
                        await self._send_logged_message(
                            application.bot,
                            "instagram_blocked",
                            chat_id=chat_id,
                            text=self._instagram_block_text(blocked_reason, str(exc)),
                            parse_mode=ParseMode.HTML,
                            reply_to_message_id=item.get("telegram_message_id"),
                        )
                elif not self._should_suppress_loop_retry_notice(job):
                    await self._send_logged_message(
                        application.bot,
                        "instagram_upload_failed",
                        chat_id=chat_id,
                        text=(
                            "⚠️ <b>Instagram upload failed</b>\n"
                            f"Job #{job_id} is still queued for retry.\n"
                            f"<code>{html.escape(str(exc)[:300])}</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=item.get("telegram_message_id"),
                    )
        finally:
            self._instagram_upload_task = None
            self._instagram_upload_job_id = None
            self._instagram_upload_started_at = None

    def _youtube_success_text(self, item: dict[str, object]) -> str:
        title = html.escape(str(item.get("title", "")))
        shorts = html.escape(str(item.get("youtube_shorts_url", "")))
        return "\n".join(
            [
                "📺 <b>Uploaded to YouTube</b>",
                "",
                f"🎬 {title}",
                "",
                "🔗 <b>YouTube URL:</b>",
                shorts,
            ]
        )

    def _youtube_queue_text(self, item: dict[str, object]) -> str:
        status = str(item.get("youtube_status", "pending"))
        if status == "uploaded":
            return self._youtube_success_text(item)
        if status == "uploading":
            return "📺 <b>YouTube upload in progress</b>\nYour video is being uploaded now."
        if status == "quota_blocked":
            return "⏳ <b>YouTube quota is over for today</b>\nThis video is saved and will upload automatically tomorrow."
        if status == "failed":
            detail = html.escape(str(item.get("last_error", "Previous upload failed")))
            return f"⚠️ <b>YouTube upload will retry</b>\n<code>{detail[:300]}</code>"
        return "📺 <b>Added to YouTube queue</b>\nYour video will upload automatically."

    def _instagram_success_text(self, item: dict[str, object]) -> str:
        url = html.escape(str(item.get("instagram_url", "")))
        width = item.get("video_width")
        height = item.get("video_height")
        lines = ["📸 <b>Uploaded to Instagram</b>", ""]
        if width and height:
            lines.extend([f"📐 <b>Verified:</b> {width}x{height}", ""])
        lines.extend(["🔗 <b>Instagram URL:</b>", url])
        return "\n".join(lines)

    def _instagram_queue_text(self, item: dict[str, object]) -> str:
        status = str(item.get("instagram_status", "pending"))
        if status == "uploaded":
            return self._instagram_success_text(item)
        if status == "uploading":
            return "📸 <b>Instagram upload in progress</b>\nYour reel is being published now."
        if status in {"blocked", "auth_blocked"}:
            return self._instagram_block_text(
                str(self.instagram_queue.status_summary().get("blocked_reason") or "unknown"),
                str(item.get("last_error", "Instagram uploads are blocked on this VPS.")),
            )
        if status == "failed":
            detail = html.escape(str(item.get("last_error", "Previous upload failed")))
            return f"⚠️ <b>Instagram upload will retry</b>\n<code>{detail[:300]}</code>"
        return "📸 <b>Added to Instagram queue</b>\nYour reel will upload automatically."

    def _instagram_block_reason(self, message: str) -> str | None:
        lowered = message.lower()
        if "not authenticated on this server" in lowered:
            return "auth"
        if "playwright install" in lowered or "browsertype.launch" in lowered or "executable doesn't exist" in lowered:
            return "runtime"
        if "only images can be posted" in lowered:
            return "runtime"
        if "create composer was not open" in lowered or "composer dialog is missing" in lowered:
            return "runtime"
        if "post entry was not found" in lowered or "create entry was not found" in lowered:
            return "runtime"
        if "no dialog action found" in lowered:
            return "runtime"
        if "not portrait" in lowered:
            return "ratio"
        return None

    def _instagram_block_text(self, reason: str, error_text: str) -> str:
        detail = f"<code>{html.escape(error_text[:300])}</code>"
        if reason == "auth":
            title = "⏳ <b>Instagram session is not authenticated on the VPS</b>"
            body = "Refresh the VPS Instagram cookies or storage before retrying uploads."
        elif reason == "runtime":
            title = "🛠️ <b>Instagram publishing is blocked on this VPS</b>"
            body = (
                "Instagram web UI state is unstable on the VPS. "
                "Review state/open_post.png, state/pre_attach.png, and state/failed_state.png, then retry."
            )
        elif reason == "ratio":
            title = "📐 <b>Instagram publishing is paused</b>"
            body = "The VPS published a non-portrait reel, so uploads were blocked to avoid bad posts."
        else:
            title = "⏳ <b>Instagram uploads are blocked on this VPS</b>"
            body = "Fix the VPS Instagram runtime before retrying uploads."
        return f"{title}\n{body}\n{detail}"

    def _should_suppress_loop_platform_success(self, job: JobDetail) -> bool:
        if job.origin != "loop":
            return False
        state = self.bot_state()
        return bool(state.loop_telegram_enabled)

    def _should_suppress_loop_retry_notice(self, job: JobDetail) -> bool:
        if job.origin != "loop":
            return False
        state = self.bot_state()
        return bool(state.loop_telegram_enabled)

    def _should_send_instagram_started_notice(self, item: dict[str, object]) -> bool:
        if not bool(item.get("instagram_enabled_for_origin")):
            return True
        return int(item.get("attempt_count") or 0) <= 1

    def _publish_upload_markup(self, job: JobDetail) -> InlineKeyboardMarkup | None:
        if job.status != "completed" or not job.output_path:
            return None
        state = self.bot_state()
        buttons: list[InlineKeyboardButton] = []
        if not (job.origin == "loop" and state.loop_youtube_enabled):
            buttons.append(InlineKeyboardButton("📺 Upload to YouTube", callback_data=f"ytup:{job.id}"))
        if not (job.origin == "loop" and state.loop_instagram_enabled):
            buttons.append(InlineKeyboardButton("📸 Upload to Instagram", callback_data=f"igup:{job.id}"))
        if not buttons:
            return None
        return InlineKeyboardMarkup([buttons])

    async def _sync_chat_actions(self, application: Application) -> None:
        desired: dict[int, str] = {}
        for batch in self.open_batches():
            if batch.status not in {"queued", "active"}:
                continue
            if batch.kind == "resend":
                desired[batch.chat_id] = ChatAction.UPLOAD_VIDEO
            else:
                desired.setdefault(batch.chat_id, ChatAction.TYPING)
        for job in self.job_service.list_delivery_pending_jobs():
            if job.chat_id is not None:
                desired[job.chat_id] = ChatAction.UPLOAD_VIDEO
        for item in self.youtube_queue.snapshot().get("items", []):
            if str(item.get("youtube_status")) == "uploading" and item.get("chat_id") is not None:
                desired[int(item["chat_id"])] = ChatAction.UPLOAD_VIDEO
        for item in self.instagram_queue.load().get("items", []):
            if str(item.get("instagram_status")) == "uploading" and item.get("chat_id") is not None:
                desired[int(item["chat_id"])] = ChatAction.UPLOAD_VIDEO

        active_chat_ids = set(self._chat_action_tasks)
        desired_chat_ids = set(desired)
        for chat_id in active_chat_ids - desired_chat_ids:
            _, task = self._chat_action_tasks.pop(chat_id)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        for chat_id, action in desired.items():
            current = self._chat_action_tasks.get(chat_id)
            if current and current[0] == action and not current[1].done():
                continue
            if current:
                current[1].cancel()
                with suppress(asyncio.CancelledError):
                    await current[1]
            task = asyncio.create_task(self._chat_action_loop(application, chat_id, action), name=f"chat-action-{chat_id}")
            self._chat_action_tasks[chat_id] = (action, task)

    async def _chat_action_loop(self, application: Application, chat_id: int, action: str) -> None:
        while True:
            try:
                await application.bot.send_chat_action(chat_id=chat_id, action=action)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to send chat action %s to %s", action, chat_id)
            await asyncio.sleep(4)

    async def _maintain_loop(self, application: Application) -> None:
        state = self.bot_state()
        if not state.loop_enabled or not state.loop_chat_id:
            return
        if self.db.count_active_jobs_by_origin("loop") > 0:
            return
        if self._loop_publications_pending():
            return
        recent = self.recent_loop_job()
        if recent:
            reference_time = recent.delivered_at or recent.completed_at or recent.updated_at or recent.created_at
            elapsed = (datetime.now(timezone.utc) - reference_time).total_seconds()
            if elapsed < state.loop_interval_seconds:
                return
        self.job_service.create_jobs(CreateJobRequest(), origin="loop", chat_id=state.loop_chat_id)

    def _loop_publications_pending(self) -> bool:
        state = self.bot_state()
        for job in self.job_service.list_jobs():
            if job.origin != "loop" or job.status != "completed":
                continue
            if not self._loop_job_ready_for_telegram(job, state):
                return True
        return False

    def _loop_job_targets(
        self,
        job: JobDetail,
        state,
    ) -> tuple[bool, bool, bool, dict[str, object] | None, dict[str, object] | None]:
    ) -> tuple[bool, bool, dict[str, object] | None]:
        youtube_item = self.youtube_queue.get_item(job.id)
        instagram_item = self.instagram_queue.get_item(job.id)
        youtube_selected = youtube_item is not None or (
            job.delivery_status in {"pending", "failed"} and bool(state.loop_youtube_enabled)
        )
        instagram_selected = instagram_item is not None or (
            job.delivery_status in {"pending", "failed"} and bool(state.loop_instagram_enabled)
        )
        telegram_selected = job.delivery_status != "skipped" and bool(state.loop_telegram_enabled)
        return telegram_selected, youtube_selected, instagram_selected, youtube_item, instagram_item
        return telegram_selected, youtube_selected, youtube_item

    def _ensure_loop_publication_jobs(
        self,
        job: JobDetail,
        state,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        _, youtube_selected, instagram_selected, youtube_item, instagram_item = self._loop_job_targets(job, state)
    ) -> dict[str, object] | None:
        _, youtube_selected, youtube_item = self._loop_job_targets(job, state)
        if youtube_selected and youtube_item is None:
            youtube_item = self.youtube_queue.enqueue_loop_job(job)
        if instagram_selected and instagram_item is None:
            instagram_item = self.instagram_queue.enqueue_job(job, instagram_enabled_for_origin=True)
        return youtube_item, instagram_item
        return youtube_item

    def _loop_job_ready_for_telegram(self, job: JobDetail, state) -> bool:
        telegram_selected, youtube_selected, instagram_selected, youtube_item, instagram_item = self._loop_job_targets(job, state)
        telegram_selected, youtube_selected, youtube_item = self._loop_job_targets(job, state)
        if youtube_selected and str((youtube_item or {}).get("youtube_status") or "") != "uploaded":
            return False
        if instagram_selected and str((instagram_item or {}).get("instagram_status") or "") != "uploaded":
            return False
        if telegram_selected:
            return job.delivery_status == "sent"
        return True

    async def _deliver_completed_jobs(self, application: Application) -> None:
        pending = self.job_service.list_delivery_pending_jobs()
        for job in pending:
            state = self.bot_state()
            if job.origin == "loop" and not state.loop_telegram_enabled:
                self.db.update_job(
                    job.id,
                    status=job.status,
                    progress=job.progress,
                    phase=job.phase,
                    message=job.message,
                    delivery_status="skipped",
                    delivery_message="Telegram delivery disabled for loop mode",
                )
                refreshed = self.job_service.get_job(job.id)
                if state.loop_youtube_enabled:
                    self.youtube_queue.enqueue_loop_job(refreshed)
                if state.loop_instagram_enabled:
                    self.instagram_queue.enqueue_job(refreshed, instagram_enabled_for_origin=True)
                continue
            if job.origin == "loop":
                youtube_item, instagram_item = self._ensure_loop_publication_jobs(job, state)
                telegram_selected, youtube_selected, instagram_selected, _, _ = self._loop_job_targets(job, state)
                youtube_item = self._ensure_loop_publication_jobs(job, state)
                telegram_selected, youtube_selected, _ = self._loop_job_targets(job, state)
                waiting_for: list[str] = []
                if youtube_selected and str((youtube_item or {}).get("youtube_status") or "") != "uploaded":
                    waiting_for.append("YouTube")
                if instagram_selected and str((instagram_item or {}).get("instagram_status") or "") != "uploaded":
                    waiting_for.append("Instagram")
                if telegram_selected and waiting_for:
                    self.db.update_job(
                        job.id,
                        status=job.status,
                        progress=job.progress,
                        phase=job.phase,
                        message=job.message,
                        delivery_status="pending",
                        delivery_message=f"Waiting for {' and '.join(waiting_for)} before Telegram delivery",
                    )
                    continue
            if job.chat_id is None:
                self.db.update_job(
                    job.id,
                    status=job.status,
                    progress=job.progress,
                    phase=job.phase,
                    message=job.message,
                    delivery_status="skipped",
                    delivery_message="No chat_id configured for delivery",
                )
                continue
            failure_count = self.db.count_delivery_attempts(job.id, status="failed")
            if failure_count >= self.config.send_retries:
                self.db.update_job(
                    job.id,
                    status=job.status,
                    progress=job.progress,
                    phase=job.phase,
                    message=job.message,
                    delivery_status="skipped",
                    delivery_message=f"Skipped after {failure_count} failed delivery attempts",
                )
                self.db.append_delivery_log(job.id, job.chat_id, "skipped", "Retry limit reached")
                continue
            if not self.db.claim_delivery(job.id):
                continue
            try:
                caption_override = self._build_loop_delivery_caption(job) if job.origin == "loop" else None
                await self.send_single_video(
                    application,
                    job,
                    chat_id=job.chat_id,
                    mark_delivery=True,
                    caption_override=caption_override,
                )
            except Exception as exc:
                logger.exception("Failed to deliver job %s", job.id)
                self.db.update_job(
                    job.id,
                    status=job.status,
                    progress=job.progress,
                    phase=job.phase,
                    message=job.message,
                    delivery_status="failed",
                    delivery_message=str(exc)[:300],
                )
                self.db.append_delivery_log(job.id, job.chat_id, "failed", str(exc)[:300])
                if job.origin == "loop":
                    await self._send_logged_message(
                        application.bot,
                        "loop_delivery_failed",
                        chat_id=job.chat_id,
                        text=f"⚠️ Loop delivery failed for Job #{job.id}\n<code>{html.escape(str(exc)[:300])}</code>",
                        parse_mode=ParseMode.HTML,
                    )

    async def _refresh_batches(self, application: Application) -> None:
        for batch in self.open_batches():
            if batch.kind == "resend":
                refreshed = row_to_batch(self.db.get_batch_row(batch.id))
                if refreshed.progress_message_id:
                    text = self._batch_progress_text(refreshed, [])
                    cached = self._batch_text_cache.get(refreshed.id)
                    if text != cached:
                        refreshed = await self._publish_batch_progress(application, refreshed, text)
                        self._batch_text_cache[refreshed.id] = text
                continue
            rows = self.db.list_jobs_for_batch(batch.id)
            jobs = [row_to_job(row) for row in rows]
            completed = sum(1 for job in jobs if job.status == "completed")
            failed = sum(1 for job in jobs if job.status in {"failed", "cancelled"})
            final = completed + failed
            sent = sum(1 for job in jobs if job.delivery_status == "sent")
            if final == 0:
                status = "queued"
            elif final < batch.requested_count or sent < completed:
                status = "active"
            elif completed > 0:
                status = "completed"
            else:
                status = "failed"
            self.db.update_batch(batch.id, completed_count=completed, failed_count=failed, status=status)
            refreshed = row_to_batch(self.db.get_batch_row(batch.id))
            if refreshed.progress_message_id:
                text = self._batch_progress_text(refreshed, jobs)
                cached = self._batch_text_cache.get(refreshed.id)
                if text != cached:
                    refreshed = await self._publish_batch_progress(application, refreshed, text)
                    self._batch_text_cache[refreshed.id] = text

    async def _publish_batch_progress(self, application: Application, batch: JobBatch, text: str) -> JobBatch:
        if batch.progress_message_id is not None:
            try:
                await application.bot.edit_message_text(
                    chat_id=batch.chat_id,
                    message_id=batch.progress_message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                return batch
            except BadRequest as exc:
                message = str(exc).lower()
                if "message is not modified" in message:
                    return batch
                logger.warning("Batch %s progress message not editable: %s", batch.id, exc)
        message = await self._send_logged_message(
            application.bot,
            "batch_progress_new_message",
            chat_id=batch.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        self.db.update_batch(batch.id, progress_message_id=message.message_id)
        return row_to_batch(self.db.get_batch_row(batch.id))

    def _batch_progress_text(self, batch: JobBatch, jobs: list[JobDetail]) -> str:
        if batch.kind == "resend":
            sent = batch.completed_count
            failed = batch.failed_count
            total = max(batch.requested_count, 1)
            progress = sent / total if batch.status == "completed" else min(0.98, sent / total)
            if failed and sent == 0 and batch.status == "failed":
                progress = 1.0
            percent = int(round(progress * 100))
            return f"{ascii_bar(progress)} {percent:>3d}%"
        completed = sum(1 for job in jobs if job.status == "completed")
        failed = sum(1 for job in jobs if job.status in {"failed", "cancelled"})
        total = max(batch.requested_count, 1)
        sent = sum(1 for job in jobs if job.delivery_status == "sent")
        current_job = next((job for job in jobs if job.status in ACTIVE_STATUSES), None)
        blended_progress = sent / total
        if current_job:
            blended_progress = ((completed + current_job.progress) / total) * 0.92
        elif completed and sent < completed:
            blended_progress = min(0.98, completed / total)
        elif completed == total and sent == total:
            blended_progress = 1.0
        elif failed and completed == 0 and batch.status == "failed":
            blended_progress = 1.0
        percent = int(round(max(0.0, min(1.0, blended_progress)) * 100))
        return f"{ascii_bar(blended_progress)} {percent:>3d}%"

    async def send_single_video(
        self,
        application: Application,
        job: JobDetail,
        *,
        chat_id: int,
        mark_delivery: bool,
        caption_override: str | None = None,
    ) -> None:
        output_path = self.config.root_dir / (job.output_path or "")
        if not output_path.exists():
            raise FileNotFoundError(f"Missing output file for job {job.id}")
        caption = caption_override or self._video_caption(job)
        if job.telegram_file_id:
            message = await application.bot.send_video(
                chat_id=chat_id,
                video=job.telegram_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                reply_markup=self._publish_upload_markup(job),
            )
        else:
            with output_path.open("rb") as handle:
                message = await application.bot.send_video(
                    chat_id=chat_id,
                    video=InputFile(handle, filename=output_path.name),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    reply_markup=self._publish_upload_markup(job),
                )
        telegram_file_id = message.video.file_id if message.video else None
        if mark_delivery:
            self.db.update_job(
                job.id,
                status=job.status,
                progress=job.progress,
                phase=job.phase,
                message=job.message,
                delivery_status="sent",
                delivery_message="Delivered to Telegram",
                delivered_at=utcnow_iso(),
                telegram_file_id=telegram_file_id,
                telegram_message_id=message.message_id,
            )
        elif telegram_file_id and not job.telegram_file_id:
            self.db.update_job(
                job.id,
                status=job.status,
                progress=job.progress,
                phase=job.phase,
                message=job.message,
                telegram_file_id=telegram_file_id,
            )
        self.db.append_delivery_log(job.id, chat_id, "sent", "Video sent")

    async def send_many_videos(self, application: Application, chat_id: int, jobs: list[JobDetail], batch_id: int | None = None) -> None:
        if not jobs:
            return
        sent = 0
        failed = 0
        try:
            for job in jobs:
                try:
                    await self.send_single_video(application, job, chat_id=chat_id, mark_delivery=False)
                    sent += 1
                except Exception as exc:
                    failed += 1
                    logger.exception("Failed to resend job %s", job.id)
                    self.db.append_delivery_log(job.id, chat_id, "failed", f"Resend failed: {str(exc)[:300]}")
                if batch_id is not None:
                    self.db.update_batch(batch_id, completed_count=sent, failed_count=failed, status="active")
            if batch_id is not None:
                self.db.update_batch(batch_id, completed_count=sent, failed_count=failed, status="completed" if sent else "failed")
                await self._send_logged_message(
                    application.bot,
                    "send_many_finished",
                    chat_id=chat_id,
                    text=f"📦 Send batch finished.\nSent: <b>{sent}</b>\nFailed: <b>{failed}</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard(),
                )
        except Exception:
            if batch_id is not None:
                self.db.update_batch(batch_id, completed_count=sent, failed_count=failed + 1, status="failed")
            raise

    def _video_caption(self, job: JobDetail) -> str:
        quote_text = job.quote.strip()
        if len(quote_text) > 700:
            quote_text = f"{quote_text[:697].rstrip()}..."
        quote = html.escape(quote_text)
        author = html.escape((job.author or "Unknown").strip())
        return "\n".join(
            [
                "🎬 <b>Motivational Video Ready</b>",
                "",
                quote,
                f"— <i>{author}</i>",
                "",
                f"<b>Job #{job.id}</b>",
            ]
        )

    def _job_title(self, job: JobDetail) -> str:
        youtube_item = self.youtube_queue.get_item(job.id)
        if youtube_item and str(youtube_item.get("title") or "").strip():
            return str(youtube_item.get("title")).strip()
        return pick_title(job.id, [], job.quote)

    def _build_loop_delivery_caption(self, job: JobDetail) -> str:
        title = html.escape(self._job_title(job))
        quote_text = job.quote.strip()
        if len(quote_text) > 360:
            quote_text = f"{quote_text[:357].rstrip()}..."
        lines = [f"🎬 <b>{title}</b>"]
        if quote_text:
            lines.extend(["", html.escape(quote_text)])
            if (job.author or "").strip():
                lines.append(f"— <i>{html.escape(job.author.strip())}</i>")
        youtube_item = self.youtube_queue.get_item(job.id)
        youtube_url = str((youtube_item or {}).get("youtube_shorts_url") or (youtube_item or {}).get("youtube_url") or "").strip()
        instagram_item = self.instagram_queue.get_item(job.id)
        instagram_url = str((instagram_item or {}).get("instagram_url") or "").strip()
        if youtube_url:
            lines.extend(["", "📺 <b>YouTube URL:</b>", html.escape(youtube_url)])
        if instagram_url:
            lines.extend(["", "📸 <b>Instagram URL:</b>", html.escape(instagram_url)])
        return "\n".join(lines[:])


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🎬 Generate Video", "🔁 Video Loop"],
            ["📚 List Videos", "📊 Status"],
            ["🛑 Stop"],
        ],
        resize_keyboard=True,
    )


def loop_interval_keyboard(selected_seconds: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for seconds in LOOP_INTERVAL_PRESETS:
        minutes = seconds // 60
        label = f"{minutes} min"
        if selected_seconds == seconds:
            label = f"✅ {label}"
        row.append(InlineKeyboardButton(label, callback_data=f"loopint:{seconds}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def loop_platform_keyboard(selected: set[str], interval_seconds: int) -> InlineKeyboardMarkup:
    def label(name: str, emoji: str) -> str:
        return f"{'✅' if name in selected else '▫️'} {emoji} {name.title()}"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"⏱️ {interval_seconds // 60} min", callback_data="loopcfg:interval")],
            [
                InlineKeyboardButton(label("telegram", "💬"), callback_data="loopplat:telegram"),
                InlineKeyboardButton(label("youtube", "📺"), callback_data="loopplat:youtube"),
                InlineKeyboardButton(label("instagram", "📸"), callback_data="loopplat:instagram"),
            ],
            [InlineKeyboardButton("✅ Start Loop", callback_data="loopconfirm:start")],
        ]
    )


async def ensure_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if runtime.is_allowed_chat(chat_id):
        return True
    if update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🚫 This bot is locked to its owner chat.")
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    text = "\n".join(
        [
            "✨ <b>AI Motivational Video Creator</b>",
            "Generate videos, run an infinite loop, list completed outputs, and manage everything from Telegram.",
            "",
            runtime.status_text(),
        ]
    )
    await update.effective_message.reply_text(text=text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    await update.effective_message.reply_text(runtime.status_text(), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


async def generate_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(str(count), callback_data=f"gen:{count}") for count in COUNT_PRESETS],
            [InlineKeyboardButton("Custom", callback_data="gen:custom")],
        ]
    )
    await update.effective_message.reply_text(
        "🎬 <b>How many videos should I generate?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def video_loop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    chat_id = update.effective_chat.id
    state = runtime.bot_state()
    if state.loop_enabled and state.loop_chat_id == chat_id:
        await update.effective_message.reply_text(
            (
                "🔁 Loop mode is already running.\n"
                f"Platforms: <b>{runtime._loop_platforms_text(state)}</b>\n"
                f"Interval: <b>{runtime._format_interval(state.loop_interval_seconds)}</b>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
        return
    context.user_data["loop_interval_seconds"] = state.loop_interval_seconds or LOOP_INTERVAL_PRESETS[0]
    context.user_data["loop_platforms"] = {"telegram"}
    await update.effective_message.reply_text(
        "🔁 <b>Select your loop interval</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=loop_interval_keyboard(context.user_data["loop_interval_seconds"]),
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    runtime.db.update_bot_state(
        loop_enabled=False,
        loop_youtube_enabled=False,
        loop_instagram_enabled=False,
        loop_telegram_enabled=False,
        stop_requested=True,
    )
    loop_jobs = [job for job in runtime.active_loop_jobs()]
    for job in loop_jobs:
        runtime.job_service.cancel_job(job.id)
    await update.effective_message.reply_text(
        "🛑 <b>Loop stop requested</b>\nQueued and active loop jobs are being cancelled now.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    await send_list_page(update.effective_chat.id, context, runtime, page=0)


async def send_list_page(chat_id: int, context: ContextTypes.DEFAULT_TYPE, runtime: TelegramBotRuntime, page: int, query_message=None) -> None:
    videos = runtime.completed_jobs()
    if not videos:
        text = "📚 No completed videos are available yet."
        if query_message:
            await query_message.edit_text(text)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=main_keyboard())
        return
    total_pages = max(1, (len(videos) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = videos[start : start + PAGE_SIZE]
    lines = [f"📚 <b>Completed Videos</b> · Page {page + 1}/{total_pages}", ""]
    keyboard_rows = []
    for job in chunk:
        title = html.escape((job.quote[:54] + "...") if len(job.quote) > 54 else job.quote)
        lines.append(f"#{job.id} · {title}")
        keyboard_rows.append([InlineKeyboardButton(f"▶ Send #{job.id}", callback_data=f"send:{job.id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"list:{page + 1}"))
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows.append(
        [
            InlineKeyboardButton("📥 Send Page", callback_data=f"page:{page}"),
            InlineKeyboardButton("📦 Send All", callback_data="all"),
        ]
    )
    markup = InlineKeyboardMarkup(keyboard_rows)
    text = "\n".join(lines)
    if query_message:
        await query_message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    query = update.callback_query
    data = query.data or ""
    if data.startswith("igup:"):
        job_id = int(data.split(":", 1)[1])
        job = runtime.job_service.get_job(job_id)
        if job.status != "completed" or not job.output_path:
            await query.answer("This video is not ready for Instagram upload yet.", show_alert=True)
            return
        stale_after_seconds = runtime._instagram_stale_after_seconds()
        runtime.instagram_queue.recover_stalled_uploading(stale_after_seconds)
        previous = runtime.instagram_queue.get_item(job_id)
        previous_status = str((previous or {}).get("instagram_status") or "")
        previous_attempts = int((previous or {}).get("attempt_count") or 0)

        if (
            previous_status == "uploading"
            and runtime._instagram_upload_task is not None
            and not runtime._instagram_upload_task.done()
            and runtime._instagram_upload_started_at is not None
        ):
            age_seconds = (datetime.now(timezone.utc) - runtime._instagram_upload_started_at).total_seconds()
            if age_seconds <= stale_after_seconds:
                logger.info(
                    "igup callback ignored because upload is already active: job_id=%s status=%s attempts=%s age_seconds=%s",
                    job_id,
                    previous_status,
                    previous_attempts,
                    int(age_seconds),
                )
                await query.answer("Instagram upload is already in progress.")
                return
            logger.warning("igup callback found stale active upload task, canceling: job_id=%s", job_id)
            runtime._instagram_upload_task.cancel()
            with suppress(asyncio.CancelledError):
                await runtime._instagram_upload_task

        item = runtime.instagram_queue.prepare_manual_retry(runtime.job_service.get_job(job_id))
        logger.info(
            "igup callback queued retry: job_id=%s previous_status=%s previous_attempts=%s new_status=%s new_attempts=%s",
            job_id,
            previous_status or "missing",
            previous_attempts,
            str(item.get("instagram_status")),
            int(item.get("attempt_count") or 0),
        )
        await runtime._maybe_process_instagram_queue(context.application)
        item = runtime.instagram_queue.get_item(job_id) or item
        status = str(item.get("instagram_status", "pending"))
        if status == "uploaded":
            await query.answer()
            await query.message.reply_text(
                runtime._instagram_success_text(item),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                reply_to_message_id=job.telegram_message_id,
            )
            return
        if status == "failed":
            await query.answer()
            await query.message.reply_text(
                runtime._instagram_queue_text(item),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                reply_to_message_id=job.telegram_message_id,
            )
            return
        if status == "blocked":
            await query.answer()
            await query.message.reply_text(
                runtime._instagram_queue_text(item),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                reply_to_message_id=job.telegram_message_id,
            )
            return
        if status == "uploading":
            await query.answer()
            await query.message.reply_text(
                (
                    "📸 <b>Instagram upload started</b>\n"
                    "The reel is now being published. This can take a few minutes on the VPS."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                reply_to_message_id=job.telegram_message_id,
            )
            return
        await query.answer()
        await query.message.reply_text(
            (
                "📸 <b>Instagram upload queued</b>\n"
                "The VPS picked up your reel. I will send the final Instagram message when publishing finishes."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
            reply_to_message_id=job.telegram_message_id,
        )
        return
    if data.startswith("ytup:"):
        job_id = int(data.split(":", 1)[1])
        job = runtime.job_service.get_job(job_id)
        if job.status != "completed" or not job.output_path:
            await query.answer("This video is not ready for YouTube upload yet.", show_alert=True)
            return
        item = runtime.youtube_queue.get_item(job_id)
        if item is None or str(item.get("youtube_status")) == "failed":
            item = runtime.youtube_queue.enqueue_job(runtime.job_service.get_job(job_id), youtube_enabled_for_origin=False)
        await runtime._maybe_process_youtube_queue(context.application)
        status = str(item.get("youtube_status", "pending"))
        if status == "uploaded":
            await query.answer()
            await query.message.reply_text(
                runtime._youtube_success_text(item),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                reply_to_message_id=job.telegram_message_id,
            )
            return
        if status == "quota_blocked":
            await query.answer()
            await query.message.reply_text(
                runtime._youtube_queue_text(item),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                reply_to_message_id=job.telegram_message_id,
            )
            return
        if status == "failed":
            await query.answer()
            await query.message.reply_text(
                runtime._youtube_queue_text(item),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                reply_to_message_id=job.telegram_message_id,
            )
            return
        if status == "uploading":
            await query.answer("YouTube upload is already in progress.")
            return
        await query.answer("Added to YouTube queue.")
        return

    await query.answer()
    if data.startswith("gen:"):
        token = data.split(":", 1)[1]
        if token == "custom":
            context.user_data["awaiting_custom_count"] = True
            await query.message.reply_text(
                "✍️ Reply with the number of videos to generate.",
                reply_markup=ForceReply(selective=True),
            )
            return
        await create_generation_batch(update.effective_chat.id, context, int(token))
        return
    if data.startswith("loopint:"):
        seconds = int(data.split(":", 1)[1])
        context.user_data["loop_interval_seconds"] = seconds
        platforms = set(context.user_data.get("loop_platforms", {"telegram"}))
        await query.message.edit_text(
            "🌐 <b>Select where each loop video should go</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=loop_platform_keyboard(platforms, seconds),
        )
        return
    if data == "loopcfg:interval":
        seconds = int(context.user_data.get("loop_interval_seconds", LOOP_INTERVAL_PRESETS[0]))
        await query.message.edit_text(
            "🔁 <b>Select your loop interval</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=loop_interval_keyboard(seconds),
        )
        return
    if data.startswith("loopplat:"):
        platform = data.split(":", 1)[1]
        selected = set(context.user_data.get("loop_platforms", {"telegram"}))
        if platform in selected:
            selected.remove(platform)
        else:
            selected.add(platform)
        if not selected:
            selected = {"telegram"}
        context.user_data["loop_platforms"] = selected
        seconds = int(context.user_data.get("loop_interval_seconds", LOOP_INTERVAL_PRESETS[0]))
        await query.message.edit_text(
            "🌐 <b>Select where each loop video should go</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=loop_platform_keyboard(selected, seconds),
        )
        return
    if data == "loopconfirm:start":
        platforms = set(context.user_data.get("loop_platforms", {"telegram"}))
        seconds = int(context.user_data.get("loop_interval_seconds", LOOP_INTERVAL_PRESETS[0]))
        runtime.db.update_bot_state(
            loop_enabled=True,
            loop_chat_id=update.effective_chat.id,
            loop_youtube_enabled="youtube" in platforms,
            loop_instagram_enabled="instagram" in platforms,
            loop_telegram_enabled="telegram" in platforms,
            loop_interval_seconds=seconds,
            loop_started_at=utcnow_iso(),
            stop_requested=False,
        )
        await query.message.reply_text(
            (
                "🔁 <b>Video loop enabled</b>\n"
                f"Platforms: <b>{', '.join(name.title() for name in sorted(platforms))}</b>\n"
                f"Interval: <b>{runtime._format_interval(seconds)}</b>\n"
                "I’ll keep generating until you send /stop."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
        return
    if data.startswith("list:"):
        await send_list_page(update.effective_chat.id, context, runtime, int(data.split(":", 1)[1]), query.message)
        return
    if data.startswith("send:"):
        job_id = int(data.split(":", 1)[1])
        job = runtime.job_service.get_job(job_id)
        await runtime.send_single_video(context.application, job, chat_id=update.effective_chat.id, mark_delivery=False)
        await query.message.reply_text(f"📤 Sent Job #{job.id}.", reply_markup=main_keyboard())
        return
    if data.startswith("page:"):
        page = int(data.split(":", 1)[1])
        videos = runtime.completed_jobs()
        start = page * PAGE_SIZE
        chunk = videos[start : start + PAGE_SIZE]
        if not chunk:
            await query.message.reply_text("Nothing to send on this page.", reply_markup=main_keyboard())
            return
        progress = await query.message.reply_text("📥 Sending this page...", reply_markup=ReplyKeyboardRemove())
        batch_id = runtime.db.create_batch(chat_id=update.effective_chat.id, kind="resend", requested_count=len(chunk), progress_message_id=progress.message_id)
        runtime._batch_text_cache.pop(batch_id, None)
        context.application.create_task(runtime.send_many_videos(context.application, update.effective_chat.id, chunk, batch_id=batch_id))
        return
    if data == "all":
        videos = runtime.completed_jobs()
        if not videos:
            await query.message.reply_text("No videos available to send.", reply_markup=main_keyboard())
            return
        progress = await query.message.reply_text("📦 Sending all completed videos in batches...", reply_markup=ReplyKeyboardRemove())
        batch_id = runtime.db.create_batch(chat_id=update.effective_chat.id, kind="resend", requested_count=len(videos), progress_message_id=progress.message_id)
        runtime._batch_text_cache.pop(batch_id, None)
        context.application.create_task(runtime.send_many_videos(context.application, update.effective_chat.id, videos, batch_id=batch_id))


async def create_generation_batch(chat_id: int, context: ContextTypes.DEFAULT_TYPE, count: int) -> None:
    runtime: TelegramBotRuntime = context.application.bot_data["runtime"]
    count = max(1, min(count, 25))
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    progress = await context.bot.send_message(
        chat_id=chat_id,
        text="🎬 Preparing your generation batch...",
        parse_mode=ParseMode.HTML,
    )
    batch_id = runtime.db.create_batch(chat_id=chat_id, kind="manual", requested_count=count, progress_message_id=progress.message_id)
    runtime.db.update_batch(batch_id, status="active")
    runtime._batch_text_cache.pop(batch_id, None)
    for _ in range(count):
        runtime.job_service.create_jobs(CreateJobRequest(), origin="manual", chat_id=chat_id, batch_id=batch_id)
    batch = row_to_batch(runtime.db.get_batch_row(batch_id))
    jobs = [runtime.job_service.get_job(job.id) for job in runtime.job_service.list_jobs() if job.batch_id == batch_id]
    text = runtime._batch_progress_text(batch, jobs)
    batch = await runtime._publish_batch_progress(context.application, batch, text)
    runtime._batch_text_cache[batch.id] = text


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    text = (update.effective_message.text or "").strip()
    if context.user_data.pop("awaiting_custom_count", False):
        if not text.isdigit():
            await update.effective_message.reply_text("Please send a whole number like 1, 3, 5, or 10.", reply_markup=main_keyboard())
            return
        await create_generation_batch(update.effective_chat.id, context, int(text))
        return
    if text == "🎬 Generate Video":
        await generate_video_command(update, context)
        return
    if text == "🔁 Video Loop":
        await video_loop_command(update, context)
        return
    if text == "📚 List Videos":
        await list_command(update, context)
        return
    if text == "📊 Status":
        await status_command(update, context)
        return
    if text == "🛑 Stop":
        await stop_command(update, context)


def build_application(config: AppConfig | None = None) -> Application:
    runtime = TelegramBotRuntime(config or load_config())
    application = (
        ApplicationBuilder()
        .token(runtime.config.telegram_bot_token or "")
        .post_init(runtime.post_init)
        .post_shutdown(runtime.post_shutdown)
        .build()
    )
    application.bot_data["runtime"] = runtime
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("generate_video", generate_video_command))
    application.add_handler(CommandHandler("video_loop", video_loop_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return application


def _release_instance_lock() -> None:
    global _INSTANCE_LOCK_HANDLE
    if _INSTANCE_LOCK_HANDLE is None:
        return
    try:
        fcntl.flock(_INSTANCE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _INSTANCE_LOCK_HANDLE.close()
    except Exception:
        pass
    _INSTANCE_LOCK_HANDLE = None


def _acquire_instance_lock(config: AppConfig) -> None:
    global _INSTANCE_LOCK_HANDLE
    lock_path = config.state_dir / "telegram-bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.seek(0)
        existing_pid = handle.read().strip()
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise RuntimeError(
                f"Another telegram bot instance is already running"
                f"{f' (pid {existing_pid})' if existing_pid else ''}."
            ) from exc
        raise
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _INSTANCE_LOCK_HANDLE = handle
    atexit.register(_release_instance_lock)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        _acquire_instance_lock(config)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return
    app = build_application(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
