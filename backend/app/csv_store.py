from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import pandas as pd

from .models import QuoteRecord

QUOTE_CANDIDATES = ("quote", "quotes", "quote_text", "quotetext", "text", "content", "message", "body")
AUTHOR_CANDIDATES = ("author", "writer", "by", "source")
RUNTIME_COLUMNS = ("status", "used_time", "output", "error")


class QuoteStore:
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self._lock = Lock()

    def _load_df(self) -> pd.DataFrame:
        df = pd.read_csv(self.csv_path, dtype=str, keep_default_na=False)
        quote_columns = [c for c in df.columns if c.strip().lower() in QUOTE_CANDIDATES]
        author_columns = [c for c in df.columns if c.strip().lower() in AUTHOR_CANDIDATES]

        if quote_columns:
            df["quote"] = df.apply(lambda row: next((str(row[c]).strip() for c in quote_columns if str(row[c]).strip() and str(row[c]).strip().lower() != "nan"), ""), axis=1)
        elif len(df.columns):
            counts = {c: df[c].astype(str).str.strip().replace({"nan": ""}).replace("", pd.NA).dropna().shape[0] for c in df.columns}
            best = max(counts.items(), key=lambda item: item[1])[0]
            df["quote"] = df[best].astype(str).fillna("").str.strip()
        else:
            df["quote"] = ""

        if author_columns:
            df["author"] = df.apply(lambda row: next((str(row[c]).strip() for c in author_columns if str(row[c]).strip() and str(row[c]).strip().lower() != "nan"), ""), axis=1)
        elif "author" not in df.columns:
            df["author"] = ""

        for column in RUNTIME_COLUMNS:
            if column not in df.columns:
                df[column] = ""

        df["quote"] = df["quote"].fillna("").astype(str).str.strip()
        df["author"] = df["author"].fillna("").astype(str).str.strip()
        df["output"] = df["output"].fillna("").astype(str).str.replace("\\", "/", regex=False)
        return df

    def normalize(self) -> pd.DataFrame:
        with self._lock:
            df = self._load_df()
            self._write(df)
            return df

    def _write(self, df: pd.DataFrame) -> None:
        temp_path = self.csv_path.with_suffix(".tmp")
        df.to_csv(temp_path, index=False)
        temp_path.replace(self.csv_path)

    def list_quotes(self) -> list[QuoteRecord]:
        df = self.normalize()
        records: list[QuoteRecord] = []
        for index, row in df.iterrows():
            records.append(QuoteRecord(
                row_id=int(index),
                quote=str(row.get("quote", "")),
                author=str(row.get("author", "")),
                status=str(row.get("status", "")),
                used_time=str(row.get("used_time", "")),
                output=str(row.get("output", "")),
                error=str(row.get("error", "")),
            ))
        return records

    def get_quote(self, row_id: int) -> QuoteRecord:
        df = self.normalize()
        if row_id < 0 or row_id >= len(df):
            raise IndexError(f"Quote row {row_id} does not exist")
        row = df.iloc[row_id]
        return QuoteRecord(
            row_id=row_id,
            quote=str(row.get("quote", "")),
            author=str(row.get("author", "")),
            status=str(row.get("status", "")),
            used_time=str(row.get("used_time", "")),
            output=str(row.get("output", "")),
            error=str(row.get("error", "")),
        )

    def choose_random_quote(self) -> QuoteRecord:
        df = self.normalize()
        candidates = [
            int(index)
            for index, row in df.iterrows()
            if str(row.get("quote", "")).strip()
        ]
        unused = [
            row_id
            for row_id in candidates
            if str(df.iloc[row_id].get("status", "")).strip().lower() != "used"
        ]
        pool = unused or candidates
        if not pool:
            raise ValueError("No quotes available")
        return self.get_quote(random.choice(pool))

    def mark_quote_output(self, row_id: int, output_path: str, status: str = "used", error: str = "") -> None:
        with self._lock:
            df = self._load_df()
            if row_id < 0 or row_id >= len(df):
                raise IndexError(f"Quote row {row_id} does not exist")
            df.at[row_id, "status"] = status
            df.at[row_id, "used_time"] = datetime.now(timezone.utc).isoformat()
            df.at[row_id, "output"] = output_path.replace("\\", "/")
            df.at[row_id, "error"] = error
            self._write(df)
