from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, check_runtime
from .csv_store import QuoteStore
from .database import Database, row_to_job, row_to_summary
from .models import CreateJobRequest, JobDetail, JobSummary, ProgressEvent
from .renderer import RenderCancelled, render_video
from .storage import AssetStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class JobContext:
    config: AppConfig
    db: Database
    assets: AssetStore
    quotes: QuoteStore


class JobService:
    def __init__(self, context: JobContext):
        self.context = context
        self._queue: queue.Queue[int] = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._work_loop, name="render-worker", daemon=True)
        self._active_lock = threading.Lock()
        self._active_cancel: dict[int, threading.Event] = {}
        self._active_job_id: int | None = None

    def start(self) -> None:
        if self._worker.is_alive():
            return
        for job_id in self.context.db.list_pending_job_ids():
            self._queue.put(job_id)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._queue.put(-1)
            self._worker.join(timeout=5)

    def create_jobs(self, payload: CreateJobRequest, *, origin: str = "manual", chat_id: int | None = None, batch_id: int | None = None) -> list[JobSummary]:
        row_ids = payload.row_ids or []
        custom_quote = (payload.custom_quote or "").strip()
        custom_author = (payload.custom_author or "").strip() or None
        darken = payload.darken if payload.darken is not None else self.context.config.default_darken
        jobs: list[JobSummary] = []
        waiting_for_slot = self.context.db.count_active_jobs() > 0
        queued_message = "Waiting for current render to finish" if waiting_for_slot else "Job accepted"
        if not row_ids and not custom_quote:
            record = self.context.quotes.choose_random_quote()
            row_ids = [record.row_id]
        if custom_quote:
            job_id = self.context.db.create_job(
                quote=custom_quote,
                author=custom_author,
                source_row_id=None,
                image_name=payload.image_name,
                music_name=payload.music_name,
                darken=darken,
                message=queued_message,
                origin=origin,
                chat_id=chat_id,
                batch_id=batch_id,
            )
            self._queue.put(job_id)
            jobs.append(self.get_job(job_id, summary=True))
            queued_message = "Waiting for current render to finish"
        for row_id in row_ids:
            record = self.context.quotes.get_quote(row_id)
            job_id = self.context.db.create_job(
                quote=record.quote,
                author=record.author or None,
                source_row_id=row_id,
                image_name=payload.image_name,
                music_name=payload.music_name,
                darken=darken,
                message=queued_message,
                origin=origin,
                chat_id=chat_id,
                batch_id=batch_id,
            )
            self._queue.put(job_id)
            jobs.append(self.get_job(job_id, summary=True))
            queued_message = "Waiting for current render to finish"
        return jobs

    def cancel_job(self, job_id: int) -> None:
        job = self.get_job(job_id)
        if job.status == "queued":
            self.context.db.cancel_job(job_id)
            return
        with self._active_lock:
            cancel_event = self._active_cancel.get(job_id)
        if cancel_event is not None and job.status in {"preparing", "rendering", "finalizing"}:
            self._progress(job_id, job.status, job.progress, job.phase, "Cancelling render")
            cancel_event.set()

    def get_job(self, job_id: int, summary: bool = False) -> JobDetail | JobSummary:
        row = self.context.db.get_job_row(job_id)
        return row_to_summary(row) if summary else row_to_job(row)

    def list_jobs(self) -> list[JobSummary]:
        return [row_to_summary(row) for row in self.context.db.list_job_rows()]

    def list_events(self, job_id: int, after_id: int = 0) -> list[ProgressEvent]:
        from .database import row_to_event
        return [row_to_event(row) for row in self.context.db.list_events(job_id, after_id)]

    def has_active_job(self) -> bool:
        return self.context.db.count_active_jobs() > 0

    def list_delivery_pending_jobs(self) -> list[JobDetail]:
        return [row_to_job(row) for row in self.context.db.list_completed_delivery_pending_rows()]

    def _progress(self, job_id: int, status: str, progress: float, phase: str, message: str, started: bool = False, completed: bool = False, output_path: str | None = None, error: str | None = None) -> None:
        self.context.db.update_job(job_id, status=status, progress=progress, phase=phase, message=message, started=started, completed=completed, output_path=output_path, error=error)

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id == -1:
                break
            try:
                self._process_job(job_id)
            except Exception:
                logger.exception("Job %s crashed", job_id)
            finally:
                self._queue.task_done()

    def _process_job(self, job_id: int) -> None:
        job = self.get_job(job_id)
        if job.status == "cancelled":
            return
        cancel_event = threading.Event()
        with self._active_lock:
            self._active_job_id = job_id
            self._active_cancel[job_id] = cancel_event

        try:
            issues = check_runtime(self.context.config)
            if issues:
                self._progress(job_id, "failed", 1.0, "Failed", "; ".join(issues), started=True, completed=True, error="; ".join(issues))
                return

            if cancel_event.is_set():
                self._progress(job_id, "cancelled", 1.0, "Cancelled", "Job cancelled before rendering", started=True, completed=True)
                return

            self._progress(job_id, "preparing", 0.08, "Preparing", "Loading project assets", started=True)
            image_path = self.context.assets.choose_image(job.image_name)
            music_path = self.context.assets.choose_music(job.music_name)
            quote_font_file = self.context.assets.default_quote_font()
            author_font_file = self.context.assets.default_author_font()
            self._progress(job_id, "preparing", 0.2, "Preparing", f"Using {image_path.name} with {music_path.name}")

            outname = f"job_{job_id}_{int(time.time())}.mp4"

            def emit(phase_status: str, progress: float, message: str) -> None:
                status = "rendering" if phase_status == "rendering" else phase_status
                phase_name = phase_status.capitalize()
                self._progress(job_id, status, progress, phase_name, message, started=True)

            outpath = render_video(
                config=self.context.config,
                image_path=image_path,
                music_path=music_path,
                quote=job.quote,
                author=job.author,
                outname=outname,
                darken=job.darken,
                quote_font_file=quote_font_file,
                author_font_file=author_font_file,
                progress_callback=emit,
                cancel_event=cancel_event,
            )
            relative_output = outpath.relative_to(self.context.config.root_dir).as_posix()
            if job.source_row_id is not None:
                self.context.quotes.mark_quote_output(job.source_row_id, relative_output)
            self._progress(job_id, "completed", 1.0, "Completed", "Video ready for preview", output_path=relative_output, started=True, completed=True)
        except RenderCancelled:
            logger.info("Job %s cancelled", job_id)
            self._progress(job_id, "cancelled", 1.0, "Cancelled", "Job cancelled", started=True, completed=True)
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            if job.source_row_id is not None:
                try:
                    self.context.quotes.mark_quote_output(job.source_row_id, "", status="failed", error=str(exc)[:200])
                except Exception:
                    logger.exception("Failed to update CSV for job %s", job_id)
            self._progress(job_id, "failed", 1.0, "Failed", str(exc), started=True, completed=True, error=str(exc))
        finally:
            with self._active_lock:
                self._active_cancel.pop(job_id, None)
                if self._active_job_id == job_id:
                    self._active_job_id = None
