"""
Read and write outputs into MBTA GCS bucket
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

BUCKET = os.getenv("GCP_BUCKET_NAME")
PREFIX = os.getenv("GCS_PREFIX", "mbta_gse/processed").strip("/")
PROJECT = os.getenv("GCP_PROJECT_ID")
KEY = os.getenv("GCP_SERVICE_ACCOUNT_KEY")
LOCAL_OUT_DIR = Path(os.getenv("LOCAL_OUT_DIR", "."))


def _bucket():
    from google.cloud import storage

    client = (storage.Client.from_service_account_json(KEY, project=PROJECT)
              if KEY else storage.Client(project=PROJECT))
    return client.bucket(BUCKET)


def location(name: str) -> str:
    return f"gs://{BUCKET}/{PREFIX}/{name}" if BUCKET else str(LOCAL_OUT_DIR / name)


def save_csv(df: pd.DataFrame, name: str, date_format: str) -> str:
    """Write df as CSV; returns where it went."""
    data = df.to_csv(index=False, date_format=date_format)
    if BUCKET:
        _bucket().blob(f"{PREFIX}/{name}").upload_from_string(data, content_type="text/csv")
    else:
        LOCAL_OUT_DIR.mkdir(parents=True, exist_ok=True)
        (LOCAL_OUT_DIR / name).write_text(data)
    return location(name)


def read_csv(name: str, parse_dates: list[str]) -> pd.DataFrame:
    """Raises FileNotFoundError if the file hasn't been written yet."""
    if BUCKET:
        blob = _bucket().blob(f"{PREFIX}/{name}")
        if not blob.exists():
            raise FileNotFoundError(location(name))
        src = io.BytesIO(blob.download_as_bytes())
    else:
        src = LOCAL_OUT_DIR / name
        if not src.exists():
            raise FileNotFoundError(str(src))
    return pd.read_csv(src, parse_dates=parse_dates)
