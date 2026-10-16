"""
Pipeline to fetch GSE data, turn it into hourly entries per station, and save to CSV (before GCS)
"""

import argparse

import pandas as pd

import fastapi_functions.gcs as gcs
from .gse_clean import hourly
from .gse_fetch import available_range, date_range, load

DAYS = 365

HOURLY_FILE = "GSE_hourly_all.csv"
HOURLY_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
RAW_DATE_FORMAT = "%Y-%m-%d"


def raw_file(month):
    """Raw rows are split into one file per service month, e.g. GSE_raw_2026-02.csv"""
    return f"GSE_raw_{month}.csv"


def save_raw(raw):
    """Upload the raw rows to the raw folder in GCS, one CSV per month."""
    months = raw["service_date"].dt.strftime("%Y-%m")
    return [gcs.save_csv(part, raw_file(m), RAW_DATE_FORMAT, gcs.RAW_PREFIX)
            for m, part in raw.groupby(months)]


def run(start=None, end=None, local=None):
    """Hourly entries per station, start..end. Defaults to the last 365 published days."""
    lo, hi = available_range(local)
    print(f"GSE data covers service dates {lo.date()} .. {hi.date()}")

    # calendar date D needs service dates D-1 and D (see gse_fetch)
    end = min(pd.Timestamp(end).normalize(), hi) if end else hi
    start = (max(pd.Timestamp(start).normalize(), lo + pd.Timedelta(days=1)) if start
             else end - pd.Timedelta(days=DAYS - 1))
    if start > end:
        raise ValueError(f"no GSE data between {start.date()} and {end.date()}")
    print(f"Dates {start.date()} .. {end.date()}")

    days = date_range(start, end)
    raw = load(days, local)
    print(f"Raw rows: {len(raw):,}")
    return {"hourly": hourly(raw, days), "raw": raw, "start": start, "end": end}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="first date, YYYY-MM-DD (default: 365 days before --end)")
    parser.add_argument("--end", help="last date, YYYY-MM-DD (default: last published date)")
    parser.add_argument("--local", help="path to a downloaded GSE.csv instead of the API")
    parser.add_argument("--upload", action="store_true", help="also write the CSVs to the GCS bucket")
    args = parser.parse_args()

    res = run(args.start, args.end, args.local)
    res["hourly"].to_csv(HOURLY_FILE, index=False, date_format=HOURLY_DATE_FORMAT)
    print(f"Saved {len(res['hourly']):,} rows to {HOURLY_FILE}")

    if args.upload:
        print("Uploaded to", gcs.save_csv(res["hourly"], HOURLY_FILE, HOURLY_DATE_FORMAT))
        raw_files = save_raw(res["raw"])
        print(f"Uploaded {len(raw_files)} raw monthly files to gs://{gcs.BUCKET}/{gcs.RAW_PREFIX}/")