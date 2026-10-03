"""
FastAPI wrapper around run_pipeline.py (source 3: MBTA gated station entries).

Run:
    fastapi run api.py            # or: fastapi dev api.py

Endpoints:
    POST /ridership/refresh               re-run the pipeline, write CSVs to GCS
        body (all optional):
            {"dates": ["2026-01-26", ...],    default: storm_days.csv
             "baseline_start": "2025-08-01",  default: 365 days before baseline_end
             "baseline_end": "2026-07-31"}    default: last published service date
    GET  /ridership/event-vs-baseline     daily total vs baseline, per station per event date
    GET  /ridership/hourly-all            hourly entries for every day in the window,
                                          with an event_day flag (~500k rows; filter it)
        filters (all GETs):
            ?date=2026-01-26
            &station=place-pktrm          stop_id or station name
            &line=Red Line                matches transfer stations too
            &hour=8                       (hourly-all) 0-23, local time
            &start=2026-01-01&end=2026-01-31   (hourly-all) date range, inclusive
            &event_day=true               (hourly-all) storm days only / false: other days
            &format=csv                   CSV download instead of JSON

A refresh pulls ~366 service dates from ArcGIS and takes a few minutes, so
deploy with a longer request timeout (e.g. gcloud run deploy --timeout=900).
GETs read the last refresh's CSVs from the bucket. Storage settings: gcs.py.
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

import gcs
from run_pipeline import (EVENTS_FILE, HOURLY_ALL_FILE, HOURLY_DATE_FORMAT, VS_DATE_FORMAT,
                          VS_FILE, read_dates_file, run)

app = FastAPI(title="MBTA storm-day gated station entries")

DEFAULT_EVENTS = Path(__file__).parent / EVENTS_FILE
_refresh_lock = threading.Lock()


class RefreshRequest(BaseModel):
    dates: list[dt.date] | None = None
    baseline_start: dt.date | None = None
    baseline_end: dt.date | None = None


def _iso(ds) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in ds]


@app.get("/")
def root():
    return {"service": app.title, "data": {"event_vs_baseline": gcs.location(VS_FILE),
                                           "hourly_all": gcs.location(HOURLY_ALL_FILE)}}


@app.post("/ridership/refresh")
def refresh(req: RefreshRequest | None = None):
    req = req or RefreshRequest()
    if not _refresh_lock.acquire(blocking=False):
        raise HTTPException(409, "a refresh is already running")
    try:
        events = req.dates or read_dates_file(DEFAULT_EVENTS)
        res = run(events, baseline_start=req.baseline_start, baseline_end=req.baseline_end)
        files = {
            "event_vs_baseline": {"path": gcs.save_csv(res.vs_baseline, VS_FILE, VS_DATE_FORMAT),
                                  "rows": len(res.vs_baseline)},
            "hourly_all": {"path": gcs.save_csv(res.hourly_all, HOURLY_ALL_FILE, HOURLY_DATE_FORMAT),
                           "rows": len(res.hourly_all)},
        }
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        _refresh_lock.release()
    return {
        "event_dates": _iso(res.event_dates),
        "dropped_dates": _iso(res.dropped_dates),
        "baseline": {"start": _iso([res.baseline_start])[0], "end": _iso([res.baseline_end])[0]},
        "stations": int(res.vs_baseline["stop_id"].nunique()),
        "files": files,
    }


def _load(name: str, date_col: str) -> pd.DataFrame:
    try:
        return gcs.read_csv(name, parse_dates=[date_col])
    except FileNotFoundError:
        raise HTTPException(404, f"{gcs.location(name)} not found; POST /ridership/refresh first")


def _filter(df, date_col, date, station, line, hour=None,
            start=None, end=None, event_day=None) -> pd.DataFrame:
    day = df[date_col].dt.normalize()
    if date:
        df = df[day == pd.Timestamp(date)]
    if start:
        df = df[day >= pd.Timestamp(start)]
    if end:
        df = df[day <= pd.Timestamp(end)]
    if event_day is not None:
        df = df[df["event_day"] == event_day]
    if station:
        s = station.lower()
        df = df[(df["stop_id"].str.lower() == s) | (df["station_name"].str.lower() == s)]
    if line:
        df = df[df["lines"].str.contains(line, case=False, regex=False)]
    if hour is not None:
        df = df[df[date_col].dt.hour == hour]
    return df


def _respond(df: pd.DataFrame, date_format: str, fmt: str, filename: str):
    if fmt == "csv":
        return Response(
            df.to_csv(index=False, date_format=date_format), media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    df = df.copy()
    for c in df.select_dtypes("datetime").columns:
        df[c] = df[c].dt.strftime(date_format)
    # NaN -> None so it serializes as JSON null
    return df.astype(object).where(df.notna(), None).to_dict(orient="records")


@app.get("/ridership/event-vs-baseline")
def event_vs_baseline(
    date: dt.date | None = None,
    station: str | None = Query(None, examples=["place-pktrm"]),
    line: str | None = Query(None, examples=["Red Line"]),
    format: Literal["json", "csv"] = "json",
):
    df = _filter(_load(VS_FILE, "date"), "date", date, station, line)
    return _respond(df, VS_DATE_FORMAT, format, VS_FILE)


@app.get("/ridership/hourly-all")
def hourly_all(
    date: dt.date | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    station: str | None = Query(None, examples=["place-pktrm"]),
    line: str | None = Query(None, examples=["Red Line"]),
    hour: int | None = Query(None, ge=0, le=23),
    event_day: bool | None = None,
    format: Literal["json", "csv"] = "json",
):
    df = _filter(_load(HOURLY_ALL_FILE, "datetime"), "datetime", date, station, line, hour,
                 start, end, event_day)
    return _respond(df, HOURLY_DATE_FORMAT, format, HOURLY_ALL_FILE)
