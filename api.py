"""
FastAPI for run_pipeline.py
source 3: MBTA gated station entries

Run: fastapi run api.py or fastapi dev api.py

Refreshing pulls 366 service dates from ArcGIS, may take a few minutes
"""

import json

import pandas as pd
from fastapi import FastAPI, HTTPException

import gcs
from run_pipeline import HOURLY_DATE_FORMAT, HOURLY_FILE, RAW_DATE_FORMAT, raw_file, run, save_raw

app = FastAPI()

START = "2025-01-01"
END = "2026-06-30"


def _day(date):
    try:
        return pd.Timestamp(date).normalize()
    except ValueError:
        raise HTTPException(422, "date must look like YYYY-MM-DD")


def _read(name, date_col, prefix=gcs.PREFIX):
    try:
        return gcs.read_csv(name, parse_dates=[date_col], prefix=prefix)
    except FileNotFoundError:
        raise HTTPException(404, f"{gcs.location(name, prefix)} not found, "
                                 "POST /ridership/refresh first")


def _filter(df, station, line, line_col):
    if station:
        s = station.lower()
        df = df[(df["stop_id"].str.lower() == s) | (df["station_name"].str.lower() == s)]
    if line:
        df = df[df[line_col].str.contains(line, case=False, regex=False)]
    return df


@app.post("/ridership/refresh")
def refresh(start: str | None = None, end: str | None = None):
    """Re-run the pipeline (default: the last 365 published days) and upload to GCS."""
    try:
        res = run(start, end)
    except ValueError as e:
        raise HTTPException(422, str(e))
    gcs.save_csv(res["hourly"], HOURLY_FILE, HOURLY_DATE_FORMAT)
    return {
        "dates": [str(res["start"].date()), str(res["end"].date())],
        "file": gcs.location(HOURLY_FILE),
        "raw_files": save_raw(res["raw"]),
    }


@app.get("/ridership/hourly-all")
def hourly_all(date: str | None = None, station: str | None = None, line: str | None = None):
    """Hourly entries per station."""
    df = _read(HOURLY_FILE, "datetime")
    if date:
        df = df[df["datetime"].dt.normalize() == _day(date)]
    df = _filter(df, station, line, "lines")
    df["datetime"] = df["datetime"].dt.strftime(HOURLY_DATE_FORMAT)
    return json.loads(df.to_json(orient="records"))


@app.get("/ridership/raw")
def raw(date: str, station: str | None = None, line: str | None = None):
    """Raw ArcGIS rows for one service date (3am to 3am), all stations, 30 minute periods."""
    day = _day(date)
    df = _read(raw_file(day.strftime("%Y-%m")), "service_date", gcs.RAW_PREFIX)
    df = _filter(df[df["service_date"] == day], station, line, "route_or_line")
    df["service_date"] = df["service_date"].dt.strftime(RAW_DATE_FORMAT)
    return json.loads(df.to_json(orient="records"))
