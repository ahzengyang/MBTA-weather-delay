"""
FastAPI for run_pipeline.py
source 3: MBTA gated station entries

Run: fastapi run api.py   or fastapi dev api.py

A refresh pulls 366 service dates from ArcGIS but may take a few minutes
"""

import json
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException

import gcs
from run_pipeline import (EVENTS_FILE, HOURLY_ALL_FILE, HOURLY_DATE_FORMAT, VS_DATE_FORMAT,
                          VS_FILE, read_dates_file, run)

app = FastAPI()

EVENTS_PATH = Path(__file__).parent / EVENTS_FILE


@app.post("/ridership/refresh")
def refresh():
    res = run(read_dates_file(EVENTS_PATH))
    gcs.save_csv(res["vs_baseline"], VS_FILE, VS_DATE_FORMAT)
    gcs.save_csv(res["hourly_all"], HOURLY_ALL_FILE, HOURLY_DATE_FORMAT)
    return {
        "event_dates": [str(d.date()) for d in res["event_dates"]],
        "dropped_dates": [str(d.date()) for d in res["dropped_dates"]],
        "baseline": [str(res["baseline_start"].date()), str(res["baseline_end"].date())],
        "files": [gcs.location(VS_FILE), gcs.location(HOURLY_ALL_FILE)],
    }


def _load(name, date_col, date_format, date, station, line):
    try:
        df = gcs.read_csv(name, parse_dates=[date_col])
    except FileNotFoundError:
        raise HTTPException(404, f"{gcs.location(name)} not found, POST /ridership/refresh first")
    if date:
        df = df[df[date_col].dt.normalize() == pd.Timestamp(date)]
    if station:
        s = station.lower()
        df = df[(df["stop_id"].str.lower() == s) | (df["station_name"].str.lower() == s)]
    if line:
        df = df[df["lines"].str.contains(line, case=False, regex=False)]
    df[date_col] = df[date_col].dt.strftime(date_format)
    return json.loads(df.to_json(orient="records"))


@app.get("/ridership/event-vs-baseline")
def event_vs_baseline(date: str | None = None, station: str | None = None, line: str | None = None):
    return _load(VS_FILE, "date", VS_DATE_FORMAT, date, station, line)


@app.get("/ridership/hourly-all")
def hourly_all(date: str | None = None, station: str | None = None, line: str | None = None):
    return _load(HOURLY_ALL_FILE, "datetime", HOURLY_DATE_FORMAT, date, station, line)
