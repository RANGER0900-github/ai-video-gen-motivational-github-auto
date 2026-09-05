from __future__ import annotations

import argparse
import time

from .config import load_config
from .csv_store import QuoteStore
from .database import Database
from .jobs import JobContext, JobService
from .models import CreateJobRequest
from .storage import AssetStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--darken", type=float, default=None)
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
        quotes = [quote.row_id for quote in quote_store.list_quotes() if quote.status.lower() != "used"][: args.count]
        jobs = job_service.create_jobs(CreateJobRequest(row_ids=quotes, darken=args.darken, image_name=args.image_name, music_name=args.music_name))
        pending = {job.id for job in jobs}
        while pending:
            done = set()
            for job_id in pending:
                job = job_service.get_job(job_id)
                print(f"[{job.id}] {job.status:10s} {job.progress:>5.0%} {job.message}")
                if job.status in {"completed", "failed", "cancelled"}:
                    done.add(job_id)
            pending -= done
            if pending:
                time.sleep(2)
    finally:
        job_service.stop()


if __name__ == "__main__":
    main()
