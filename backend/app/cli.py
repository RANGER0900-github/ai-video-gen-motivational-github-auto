from __future__ import annotations

import argparse
import random
import time

from .config import load_config
from .csv_store import QuoteStore
from .database import Database
from .jobs import JobContext, JobService
from .models import CreateJobRequest
from .storage import AssetStore


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI video generation runner")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--darken", type=float, default=None)
    parser.add_argument("--media-type", choices=["auto", "video", "image"], default="auto", help="Background media type (auto adheres to 5:1 daily ratio)")
    parser.add_argument("--video-name", default=None, help="Specific video background filename")
    parser.add_argument("--image-name", default=None)
    parser.add_argument("--music-name", default=None)
    parser.add_argument("--workers", type=int, default=1, help="Reserved for compatibility; rendering currently uses one background worker")
    args = parser.parse_args()

    config = load_config()
    database = Database(config.db_path)
    quote_store = QuoteStore(config.quotes_csv)
    job_service = JobService(JobContext(config=config, db=database, assets=AssetStore(config), quotes=quote_store))
    job_service.start()
    try:
        all_records = quote_store.list_quotes()
        if not all_records:
            raise ValueError("No quotes found in quotes.csv")

        unused = [q.row_id for q in all_records if q.status.lower() != "used"]
        if len(unused) >= args.count:
            quotes = unused[: args.count]
        elif unused:
            all_ids = [q.row_id for q in all_records]
            quotes = unused + [q_id for q_id in all_ids if q_id not in unused][: args.count - len(unused)]
        else:
            all_ids = [q.row_id for q in all_records]
            quotes = random.sample(all_ids, min(args.count, len(all_ids)))

        jobs = job_service.create_jobs(
            CreateJobRequest(
                row_ids=quotes,
                darken=args.darken,
                image_name=args.image_name,
                video_name=args.video_name,
                media_type=args.media_type,
                music_name=args.music_name,
            )
        )
        pending = {job.id for job in jobs}
        failed = set()
        while pending:
            done = set()
            for job_id in pending:
                job = job_service.get_job(job_id)
                print(f"[{job.id}] {job.status:10s} {job.progress:>5.0%} {job.message}")
                if job.status in {"completed", "failed", "cancelled"}:
                    done.add(job_id)
                    if job.status != "completed":
                        failed.add(job_id)
            pending -= done
            if pending:
                time.sleep(2)
        if failed:
            print(f"❌ Error: Job(s) failed: {failed}")
            raise SystemExit(1)
    finally:
        job_service.stop()


if __name__ == "__main__":
    main()
