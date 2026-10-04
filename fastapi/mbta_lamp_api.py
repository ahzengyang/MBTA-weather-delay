#!/usr/bin/env python3
"""
mbta_lamp_api.py
================

FastAPI service for the MBTA LAMP station x hour delay panel. Every number it
returns is produced by source1_mbta_lamp_delay.py (same fetch, same derivation,
same exclusions, same aggregation), so a range requested here matches the CSV
the script uploads for that range.

Endpoints
---------
GET  /panel?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD[&format=csv]
     The panel for an inclusive date range, as JSON (default) or CSV.
POST /panel/upload?start_date=...&end_date=...
     Builds the same panel and uploads it to GCS, exactly like the script.
GET  /dates      Service dates LAMP has published (from index.csv).
GET  /health     Liveness check.

Interactive docs: http://127.0.0.1:8000/docs

Run (from the folder holding this file, source1_mbta_lamp_delay.py and
user_definition.py):

    pip install fastapi uvicorn
    uvicorn mbta_lamp_api:app --reload

Settings (environment variables, optional)
    MBTA_API_MAX_RANGE_DAYS  longest range one request may ask for (default 500)
    MBTA_API_CACHE_DAYS      processed days kept in memory (default 62)
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from google.api_core.exceptions import GoogleAPIError

import source1_mbta_lamp_delay as lamp

MAX_RANGE_DAYS = int(os.getenv("MBTA_API_MAX_RANGE_DAYS", "500"))
CACHE_DAYS = int(os.getenv("MBTA_API_CACHE_DAYS", "62"))
MIN_AGE_DAYS = 2   # same default as the script's --min-age-days

app = FastAPI(
    title="MBTA LAMP Delay API",
    version="1.0",
    description="Per-station, per-local-hour excess travel time and headway from MBTA LAMP "
                "subway performance data. Same logic and output as source1_mbta_lamp_delay.py.",
)


# --------------------------------------------------------------------------- #
# Panel construction (wraps the script's own functions)
# --------------------------------------------------------------------------- #
def _complete_cutoff() -> date:
    """Dates after this may still be growing on LAMP's side."""
    return datetime.now(ZoneInfo(lamp.LOCAL_TZ)).date() - timedelta(days=MIN_AGE_DAYS)


@lru_cache(maxsize=CACHE_DAYS)
def _cached_day(d: date) -> pd.DataFrame:
    """Processed day, cached. Only complete days are routed here; failures aren't cached."""
    return lamp.process_day(d, lamp._session())


def _validate_range(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        raise HTTPException(400, f"end_date ({end_date}) is before start_date ({start_date}).")
    n_days = (end_date - start_date).days + 1
    if n_days > MAX_RANGE_DAYS:
        raise HTTPException(400, f"Range is {n_days} days; the maximum per request is "
                                 f"{MAX_RANGE_DAYS}. Split it into smaller requests.")
    return [start_date + timedelta(days=i) for i in range(n_days)]


def build_panel(start_date: date, end_date: date) -> dict:
    """Same steps as the script's collect() + station_hour_panel(), with per-day caching."""
    days = _validate_range(start_date, end_date)
    cutoff = _complete_cutoff()
    sess = lamp._session()

    frames: list[pd.DataFrame] = []
    loaded: list[str] = []
    skipped: list[dict] = []

    for i, d in enumerate(days, 1):
        print(f"\n[{i}/{len(days)}] Fetching {d} ...")
        try:
            day_frame = _cached_day(d) if d <= cutoff else lamp.process_day(d, sess)
        except (FileNotFoundError, requests.RequestException) as exc:
            print(f"  skipped: {exc}")
            skipped.append({"date": d.isoformat(), "reason": str(exc)})
            continue
        except Exception as exc:  # one malformed file should not sink the request
            print(f"  skipped: could not process {d}: {type(exc).__name__}: {exc}")
            skipped.append({"date": d.isoformat(),
                            "reason": f"could not process: {type(exc).__name__}: {exc}"})
            continue
        frames.append(day_frame)
        loaded.append(d.isoformat())

    if not frames:
        raise HTTPException(404, {"message": "No data could be loaded for any date in the range.",
                                  "dates_skipped": skipped})

    # Aggregate all days together, as the script does, so a station-hour that
    # straddles two service dates (late-night service) is a single row.
    panel = lamp.station_hour_panel(pd.concat(frames, ignore_index=True))

    warnings = []
    if end_date > cutoff:
        warnings.append(f"Dates after {cutoff} may be incomplete (LAMP files for recent days grow live).")

    return {
        "panel": panel,
        "filename": f"mbta_station_hour_panel_{lamp.panel_label(start_date, end_date)}.csv",
        "summary": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "dates_requested": len(days),
            "dates_loaded": loaded,
            "dates_skipped": skipped,
            "warnings": warnings,
            "n_rows": len(panel),
            "n_stations": int(panel["parent_station"].nunique()),
            "n_hours": int(panel["hour_local"].nunique()),
        },
    }


def _panel_records(panel: pd.DataFrame) -> list[dict]:
    """Rows as JSON records. Timestamps are written exactly as in the CSV
    (e.g. '2025-03-08 05:00:00-05:00'); NaN becomes null."""
    out = panel.copy()
    for col in ("hour_local", "hour_utc"):
        out[col] = out[col].astype(str)
    return json.loads(out.to_json(orient="records", double_precision=15))


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
StartDate = Annotated[date, Query(description="First service date (inclusive), YYYY-MM-DD",
                                  examples=["2025-03-01"])]
EndDate = Annotated[date, Query(description="Last service date (inclusive), YYYY-MM-DD",
                                examples=["2025-03-31"])]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/dates")
def published_dates() -> dict:
    """Service dates listed in LAMP's index.csv."""
    try:
        dates = lamp.list_service_dates()
    except (requests.RequestException, RuntimeError) as exc:
        raise HTTPException(502, f"Could not read LAMP index: {exc}")
    return {"count": len(dates), "first": dates[0].isoformat(), "last": dates[-1].isoformat(),
            "complete_through": min(dates[-1], _complete_cutoff()).isoformat(),
            "dates": [d.isoformat() for d in dates]}


@app.get("/panel")
def get_panel(
    start_date: StartDate,
    end_date: EndDate,
    fmt: Annotated[Literal["json", "csv"], Query(alias="format", description="json or csv")] = "json",
):
    """
    Station x local-hour delay panel for an inclusive date range.

    JSON: a summary (dates loaded/skipped, counts) plus `data`, one record per
    (parent_station, hour_local). CSV: the same file the script uploads, with
    the loaded/skipped dates in X-Dates-* headers.
    """
    result = build_panel(start_date, end_date)
    panel, summary = result["panel"], result["summary"]

    if fmt == "csv":
        return Response(
            content=panel.to_csv(index=False),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{result["filename"]}"',
                "X-Dates-Loaded": ",".join(summary["dates_loaded"]),
                "X-Dates-Skipped": ",".join(s["date"] for s in summary["dates_skipped"]),
            },
        )
    return {**summary, "columns": list(panel.columns), "data": _panel_records(panel)}


@app.post("/panel/upload")
def upload_panel(
    start_date: StartDate,
    end_date: EndDate,
    prefix: Annotated[str, Query(description="Folder inside the bucket")] = lamp.DEFAULT_PREFIX,
) -> dict:
    """Build the panel and upload it to GCS at PREFIX/processed/<filename>, as the script does."""
    try:
        lamp.check_gcp_config()
        bucket = lamp.get_bucket()
    except (RuntimeError, FileNotFoundError, ValueError, GoogleAPIError) as exc:
        raise HTTPException(500, f"GCP setup failed: {exc}")

    result = build_panel(start_date, end_date)
    blob_name = f"{prefix.strip('/')}/processed/{result['filename']}"
    try:
        lamp.upload_bytes(bucket, blob_name, result["panel"].to_csv(index=False), "text/csv")
    except GoogleAPIError as exc:
        # Completed days stay cached, so retrying after fixing the problem is fast.
        raise HTTPException(502, f"Upload to gs://{bucket.name}/{blob_name} failed: {exc}")
    return {**result["summary"], "uploaded": f"gs://{bucket.name}/{blob_name}"}
