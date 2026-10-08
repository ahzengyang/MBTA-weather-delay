"""
Read and write csv outputs into MBTA GCS bucket
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
PROJECT = os.getenv("GCP_PROJECT_ID")
KEY = os.getenv("GCP_SERVICE_ACCOUNT_KEY")


def _bucket():
    credentials = service_account.Credentials.from_service_account_file(KEY) if KEY else None
    client = storage.Client(project=PROJECT, credentials=credentials)
    return client.bucket(BUCKET)


def location(name):
    return f"gs://{BUCKET}/{PREFIX}/{name}"


def save_csv(df, name, date_format):
    data = df.to_csv(index=False, date_format=date_format)
    _bucket().blob(f"{PREFIX}/{name}").upload_from_string(data, content_type="text/csv")
    return location(name)


def read_csv(name, parse_dates):
    blob = _bucket().blob(f"{PREFIX}/{name}")
    if not blob.exists():
        raise FileNotFoundError(location(name))
    return pd.read_csv(io.BytesIO(blob.download_as_bytes()), parse_dates=parse_dates)
