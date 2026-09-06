from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import BotState, JobBatch, JobDetail, JobSummary, ProgressEvent


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  progress REAL NOT NULL,
  phase TEXT NOT NULL,
  message TEXT NOT NULL,
  quote TEXT NOT NULL,
  author TEXT,
  source_row_id INTEGER,
  image_name TEXT,
  music_name TEXT,
  darken REAL NOT NULL,
  output_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  progress REAL NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS bot_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  loop_enabled INTEGER NOT NULL DEFAULT 0,
  loop_chat_id INTEGER,
  loop_youtube_enabled INTEGER NOT NULL DEFAULT 0,
  loop_instagram_enabled INTEGER NOT NULL DEFAULT 0,
  loop_telegram_enabled INTEGER NOT NULL DEFAULT 1,
  loop_interval_seconds INTEGER NOT NULL DEFAULT 600,
  loop_started_at TEXT,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  last_startup_at TEXT
);
CREATE TABLE IF NOT EXISTS job_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  requested_count INTEGER NOT NULL,
  completed_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  progress_message_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);
"""

JOB_EXTRA_COLUMNS: dict[str, str] = {
    "origin": "TEXT NOT NULL DEFAULT 'manual'",
    "chat_id": "INTEGER",
    "batch_id": "INTEGER",
    "delivery_status": "TEXT NOT NULL DEFAULT 'pending'",
    "delivery_message": "TEXT",
    "delivered_at": "TEXT",
    "telegram_file_id": "TEXT",
    "telegram_message_id": "INTEGER",
    "media_type": "TEXT",
}

BOT_STATE_EXTRA_COLUMNS: dict[str, str] = {
    "loop_youtube_enabled": "INTEGER NOT NULL DEFAULT 0",
    "loop_instagram_enabled": "INTEGER NOT NULL DEFAULT 0",
    "loop_telegram_enabled": "INTEGER NOT NULL DEFAULT 1",
    "loop_interval_seconds": "INTEGER NOT NULL DEFAULT 600",
}


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_job_columns(conn)
            self._ensure_bot_state_columns(conn)
            conn.execute("INSERT OR IGNORE INTO bot_state (id) VALUES (1)")

    def _ensure_job_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, ddl in JOB_EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

    def _ensure_bot_state_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(bot_state)").fetchall()}
        for name, ddl in BOT_STATE_EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE bot_state ADD COLUMN {name} {ddl}")

    def create_job(self, quote: str, author: str | None, source_row_id: int | None, image_name: str | None, music_name: str | None, darken: float, message: str = "Job accepted", origin: str = "manual", chat_id: int | None = None, batch_id: int | None = None, delivery_status: str = "pending", media_type: str | None = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (status, progress, phase, message, quote, author, source_row_id, image_name, music_name, darken, created_at, updated_at, origin, chat_id, batch_id, delivery_status, media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("queued", 0.0, "Queued", message, quote, author, source_row_id, image_name, music_name, darken, now, now, origin, chat_id, batch_id, delivery_status, media_type),
            )
            job_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO events (job_id, status, phase, progress, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, "queued", "Queued", 0.0, message, now),
            )
            return job_id

    def count_active_jobs(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'preparing', 'rendering', 'finalizing')"
            ).fetchone()
        return int(row[0]) if row else 0

    def count_active_jobs_by_origin(self, origin: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE origin = ? AND status IN ('queued', 'preparing', 'rendering', 'finalizing')",
                (origin,),
            ).fetchone()
        return int(row[0]) if row else 0

    def update_job(self, job_id: int, *, status: str, progress: float, phase: str, message: str, output_path: str | None = None, error: str | None = None, started: bool = False, completed: bool = False, delivery_status: str | None = None, delivery_message: str | None = None, delivered_at: str | None = None, telegram_file_id: str | None = None, telegram_message_id: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            current = conn.execute("SELECT started_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
            started_at = current["started_at"] if current else None
            if started and not started_at:
                started_at = now
            completed_at = now if completed else None
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, progress = ?, phase = ?, message = ?, output_path = COALESCE(?, output_path), error = ?, updated_at = ?, started_at = COALESCE(?, started_at), completed_at = COALESCE(?, completed_at), delivery_status = COALESCE(?, delivery_status), delivery_message = COALESCE(?, delivery_message), delivered_at = COALESCE(?, delivered_at), telegram_file_id = COALESCE(?, telegram_file_id), telegram_message_id = COALESCE(?, telegram_message_id)
                WHERE id = ?
                """,
                (status, progress, phase, message, output_path, error, now, started_at, completed_at, delivery_status, delivery_message, delivered_at, telegram_file_id, telegram_message_id, job_id),
            )
            conn.execute(
                "INSERT INTO events (job_id, status, phase, progress, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, status, phase, progress, message, now),
            )

    def cancel_job(self, job_id: int) -> None:
        self.update_job(job_id, status="cancelled", progress=0.0, phase="Cancelled", message="Job cancelled before rendering", completed=True)

    def get_job_row(self, job_id: int):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return row

    def list_job_rows(self):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()

    def list_events(self, job_id: int, after_id: int = 0):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM events WHERE job_id = ? AND id > ? ORDER BY id ASC", (job_id, after_id)
            ).fetchall()

    def list_pending_job_ids(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'preparing', 'rendering', 'finalizing') ORDER BY created_at ASC").fetchall()
        return [int(row[0]) for row in rows]

    def list_job_rows_by_statuses(self, statuses: tuple[str, ...]):
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as conn:
            return conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
                statuses,
            ).fetchall()

    def list_completed_delivery_pending_rows(self):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'completed'
                  AND chat_id IS NOT NULL
                  AND delivery_status IN ('pending', 'failed')
                ORDER BY completed_at ASC, id ASC
                """
            ).fetchall()

    def claim_delivery(self, job_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET delivery_status = 'sending',
                    delivery_message = 'Sending to Telegram',
                    updated_at = ?
                WHERE id = ?
                  AND status = 'completed'
                  AND delivery_status IN ('pending', 'failed')
                """,
                (now, job_id),
            )
            return cursor.rowcount > 0

    def list_jobs_for_batch(self, batch_id: int):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at ASC, id ASC",
                (batch_id,),
            ).fetchall()

    def create_batch(self, chat_id: int, kind: str, requested_count: int, progress_message_id: int | None = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO job_batches (chat_id, kind, requested_count, progress_message_id, created_at, updated_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'queued')
                """,
                (chat_id, kind, requested_count, progress_message_id, now, now),
            )
            return int(cursor.lastrowid)

    def get_batch_row(self, batch_id: int):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM job_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return row

    def list_open_batches(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM job_batches WHERE status IN ('queued', 'active') ORDER BY created_at ASC"
            ).fetchall()

    def update_batch(self, batch_id: int, *, completed_count: int | None = None, failed_count: int | None = None, status: str | None = None, progress_message_id: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM job_batches WHERE id = ?", (batch_id,)).fetchone()
            if current is None:
                raise KeyError(batch_id)
            conn.execute(
                """
                UPDATE job_batches
                SET completed_count = ?, failed_count = ?, status = ?, progress_message_id = COALESCE(?, progress_message_id), updated_at = ?
                WHERE id = ?
                """,
                (
                    completed_count if completed_count is not None else current["completed_count"],
                    failed_count if failed_count is not None else current["failed_count"],
                    status or current["status"],
                    progress_message_id,
                    now,
                    batch_id,
                ),
            )

    def get_bot_state_row(self):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
        if row is None:
            raise KeyError("bot_state")
        return row

    def update_bot_state(self, *, loop_enabled: bool | None = None, loop_chat_id: int | None = None, loop_youtube_enabled: bool | None = None, loop_telegram_enabled: bool | None = None, loop_interval_seconds: int | None = None, loop_started_at: str | None = None, stop_requested: bool | None = None, last_startup_at: str | None = None) -> None:
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
            if current is None:
                conn.execute("INSERT INTO bot_state (id) VALUES (1)")
                current = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
            conn.execute(
                """
                UPDATE bot_state
                SET loop_enabled = ?, loop_chat_id = ?, loop_youtube_enabled = ?, loop_telegram_enabled = ?, loop_interval_seconds = ?, loop_started_at = ?, stop_requested = ?, last_startup_at = ?
                WHERE id = 1
                """,
                (
                    int(loop_enabled if loop_enabled is not None else current["loop_enabled"]),
                    loop_chat_id if loop_chat_id is not None else current["loop_chat_id"],
                    int(loop_youtube_enabled if loop_youtube_enabled is not None else current["loop_youtube_enabled"]),
                    int(loop_telegram_enabled if loop_telegram_enabled is not None else current["loop_telegram_enabled"]),
                    int(loop_interval_seconds if loop_interval_seconds is not None else current["loop_interval_seconds"]),
                    loop_started_at if loop_started_at is not None else current["loop_started_at"],
                    int(stop_requested if stop_requested is not None else current["stop_requested"]),
                    last_startup_at if last_startup_at is not None else current["last_startup_at"],
                ),
            )

    def append_delivery_log(self, job_id: int, chat_id: int, status: str, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO delivery_log (job_id, chat_id, status, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, chat_id, status, message, now),
            )

    def count_delivery_attempts(self, job_id: int, status: str | None = None) -> int:
        with self.connect() as conn:
            if status is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM delivery_log WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM delivery_log WHERE job_id = ? AND status = ?",
                    (job_id, status),
                ).fetchone()
        return int(row[0]) if row else 0

    def update_job_output_path(self, job_id: int, output_path: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET output_path = ?, updated_at = ? WHERE id = ?",
                (output_path, now, job_id),
            )


def row_to_job(row) -> JobDetail:
    return JobDetail(
        id=int(row["id"]),
        status=row["status"],
        progress=float(row["progress"]),
        phase=row["phase"],
        message=row["message"],
        quote=row["quote"],
        author=row["author"],
        image_name=row["image_name"],
        media_type=row["media_type"] if "media_type" in row.keys() else None,
        music_name=row["music_name"],
        output_path=row["output_path"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        error=row["error"],
        source_row_id=row["source_row_id"],
        darken=float(row["darken"]),
        origin=row["origin"] or "manual",
        chat_id=int(row["chat_id"]) if row["chat_id"] is not None else None,
        batch_id=int(row["batch_id"]) if row["batch_id"] is not None else None,
        delivery_status=row["delivery_status"] or "pending",
        delivery_message=row["delivery_message"],
        delivered_at=datetime.fromisoformat(row["delivered_at"]) if row["delivered_at"] else None,
        telegram_file_id=row["telegram_file_id"],
        telegram_message_id=int(row["telegram_message_id"]) if row["telegram_message_id"] is not None else None,
    )


def row_to_summary(row) -> JobSummary:
    job = row_to_job(row)
    return JobSummary(**job.model_dump(exclude={"source_row_id", "darken"}))


def row_to_event(row) -> ProgressEvent:
    return ProgressEvent(
        id=int(row["id"]),
        job_id=int(row["job_id"]),
        status=row["status"],
        phase=row["phase"],
        progress=float(row["progress"]),
        message=row["message"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def row_to_bot_state(row) -> BotState:
    return BotState(
        id=int(row["id"]),
        loop_enabled=bool(row["loop_enabled"]),
        loop_chat_id=int(row["loop_chat_id"]) if row["loop_chat_id"] is not None else None,
        loop_youtube_enabled=bool(row["loop_youtube_enabled"]) if row["loop_youtube_enabled"] is not None else False,
        loop_instagram_enabled=bool(row["loop_instagram_enabled"]) if row["loop_instagram_enabled"] is not None else False,
        loop_telegram_enabled=bool(row["loop_telegram_enabled"]) if row["loop_telegram_enabled"] is not None else True,
        loop_interval_seconds=int(row["loop_interval_seconds"]) if row["loop_interval_seconds"] is not None else 600,
        loop_started_at=datetime.fromisoformat(row["loop_started_at"]) if row["loop_started_at"] else None,
        stop_requested=bool(row["stop_requested"]),
        last_startup_at=datetime.fromisoformat(row["last_startup_at"]) if row["last_startup_at"] else None,
    )


def row_to_batch(row) -> JobBatch:
    return JobBatch(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        kind=row["kind"],
        requested_count=int(row["requested_count"]),
        completed_count=int(row["completed_count"]),
        failed_count=int(row["failed_count"]),
        status=row["status"],
        progress_message_id=int(row["progress_message_id"]) if row["progress_message_id"] is not None else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
