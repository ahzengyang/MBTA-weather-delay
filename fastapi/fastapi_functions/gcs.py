"""
Read and write ridership csv outputs into MBTA GCS bucket
"""

import io
import os

import pandas as pd
from dotenv import load_dotenv
from google.cloud import storage
from google.oauth2 import service_account

load_dotenv()

BUCKET = os.getenv("GCP_BUCKET_NAME")
PREFIX = os.getenv("GCS_PREFIX", "mbta_gse/processed").strip("/")
RAW_PREFIX = os.getenv("GCS_RAW_PREFIX", "mbta_gse/raw").strip("/")
PROJECT = os.getenv("GCP_PROJECT_ID")
KEY = os.getenv("GCP_SERVICE_ACCOUNT_KEY")


def _bucket():
    credentials = service_account.Credentials.from_service_account_file(KEY) if KEY else None
    client = storage.Client(project=PROJECT, credentials=credentials)
    return client.bucket(BUCKET)


def location(name, prefix=PREFIX):
    return f"gs://{BUCKET}/{prefix}/{name}"


def save_csv(df, name, date_format, prefix=PREFIX):
    data = df.to_csv(index=False, date_format=date_format)
    _bucket().blob(f"{prefix}/{name}").upload_from_string(data, content_type="text/csv")
    return location(name, prefix)


def read_csv(name, parse_dates, prefix=PREFIX):
    blob = _bucket().blob(f"{prefix}/{name}")
    if not blob.exists():
        raise FileNotFoundError(location(name, prefix))
    return pd.read_csv(io.BytesIO(blob.download_as_bytes()), parse_dates=parse_dates)


def save_bytes(data, name, content_type, prefix=PREFIX):
    """Upload in-memory bytes (parquet, gzip, ...) to gs://BUCKET/prefix/name."""
    _bucket().blob(f"{prefix}/{name}").upload_from_string(data, content_type=content_type)
    return location(name, prefix)
