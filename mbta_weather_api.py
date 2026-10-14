"""One large combined API for all four data sources for the project in MBTA delay data.

Each source keeps its own prefix in order for the endpoints to know where they came from:
   - /stations was "station_api.py"
   - /delays was "mbta_lamp_api.py"
   - /weather was "fastapi_weather.py"
   - /ridership was "api.py"

How to run: fastapi run mbta_weather_api.py

Needs the following files with it: user_definition.py, station_data.py, source1_mbta_lamp_delay.py, weather_openmeteo.py, run_pipeline.py, gcs.py,
gse_fetch.py, gse_clean.py, gse_baseline.py, stations.py
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from google.api_core.exceptions import GoogleAPIError
from google.cloud import storage
from pydantic import BaseModel

import gcs
import source1_mbta_lamp_delay as lamp
import station_data
from run_pipeline import (EVENTS_FILE, HOURLY_ALL_FILE, HOURLY_DATE_FORMAT,
                          RAW_DATE_FORMAT, VS_DATE_FORMAT, VS_FILE, raw_file,
                          read_dates_file, run, save_raw)
from weather_openmeteo import (WeatherFetchError, fetch_weather,
                               save_processed)
from user_definition import *

MAX_RANGE_DAYS = int(os.getenv("MBTA_API_MAX_RANGE_DAYS", "500"))
CACHE_DAYS = int(os.getenv("MBTA_API_CACHE_DAYS", "62"))
MIN_AGE_DAYS = 2

EVENTS_PATH = Path(__file__).parent / EVENTS_FILE

app = FastAPI(
    title="MBTA Weather Delay API",
    version="1.0",
    description="Station reference table, LAMP delay panel, Open-Meteo hourly "
                "weather and gated station entries behind one API.",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok",
            "sources": ["stations", "delays", "weather", "ridership"]}


def build_station_table():
    stops_df = station_data.get_raw_csv(mbta_gtfs_url, "stops.txt")
    levels_df = station_data.get_raw_csv(mbta_gtfs_url, "levels.txt")

    underground_flags = station_data.get_underground_flags(stops_df, levels_df)
    processed_stops_df = station_data.process_subway_stations(stops_df,
                                                              underground_flags)
    return processed_stops_df


@app.get("/stations")
def get_stations(
    fmt: Annotated[Literal["json", "csv"], Query(alias="format", description="json or csv")] = "json",
):
    try:
        station_df = build_station_table()
    except Exception as exc:
        raise HTTPException(502, f"Could not build station table: {exc}")

    if fmt == "csv":
        return Response(
            content=station_df.to_csv(index=False),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="mbta_subway_stations.csv"'},
        )
    return {
        "n_stations": len(station_df),
        "columns": list(station_df.columns),
        "data": station_df.to_dict(orient="records"),
    }


@app.post("/stations/upload")
def upload_stations(
    blob_path: Annotated[str, Query(description="Destination path inside the bucket")]
    = f"station_processed/{station_data_file_name}",
) -> dict:
    try:
        station_df = build_station_table()
    except Exception as exc:
        raise HTTPException(502, f"Could not build station table: {exc}")

    try:
        station_data.upload_to_gcs(
            station_df,
            service_account_file_path,
            project_id,
            bucket_name,
            blob_path,
        )
    except (GoogleAPIError, OSError, ValueError) as exc:
        raise HTTPException(502, f"Upload to gs://{bucket_name}/{blob_path} failed: {exc}")

    return {
        "status": "success",
        "n_stations": len(station_df),
        "uploaded": f"gs://{bucket_name}/{blob_path}",
    }


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
        except Exception as exc:
            print(f"  skipped: could not process {d}: {type(exc).__name__}: {exc}")
            skipped.append({"date": d.isoformat(),
                            "reason": f"could not process: {type(exc).__name__}: {exc}"})
            continue
        frames.append(day_frame)
        loaded.append(d.isoformat())

    if not frames:
        raise HTTPException(404, {"message": "No data could be loaded for any date in the range.",
                                  "dates_skipped": skipped})

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


StartDate = Annotated[date, Query(description="First service date (inclusive), YYYY-MM-DD",
                                  examples=["2025-03-01"])]
EndDate = Annotated[date, Query(description="Last service date (inclusive), YYYY-MM-DD",
                                examples=["2025-03-31"])]


@app.get("/delays/dates")
def published_dates() -> dict:
    """Service dates listed in LAMP's index.csv."""
    try:
        dates = lamp.list_service_dates()
    except (requests.RequestException, RuntimeError) as exc:
        raise HTTPException(502, f"Could not read LAMP index: {exc}")
    return {"count": len(dates), "first": dates[0].isoformat(), "last": dates[-1].isoformat(),
            "complete_through": min(dates[-1], _complete_cutoff()).isoformat(),
            "dates": [d.isoformat() for d in dates]}


@app.get("/delays/panel")
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


@app.post("/delays/panel/upload")
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
        raise HTTPException(502, f"Upload to gs://{bucket.name}/{blob_name} failed: {exc}")
    return {**result["summary"], "uploaded": f"gs://{bucket.name}/{blob_name}"}


class UploadInput(BaseModel):
    bucket: str = bucket_name


def upload_file_to_gcs(bucket: str,
                       file_name: str,
                       file_path: str) -> None:
    client = storage.Client()
    gcs_bucket = client.bucket(bucket)
    file = gcs_bucket.blob(file_name)
    file.upload_from_filename(file_path)


@app.get("/weather")
def get_weather(station: str,
                start: str = start_date,
                end: str = end_date):
    stations = pd.read_csv(stations_file)
    matched = stations[stations["station_name"].str.lower() ==
                       station.lower()]
    if matched.empty:
        raise HTTPException(status_code=404,
                            detail=f"Unknown station {station}.")
    selected = matched.iloc[0]

    try:
        weather = fetch_weather(selected["latitude"],
                                selected["longitude"],
                                start,
                                end,
                                include_visibility=True)
        weather.insert(0, "location", selected["station_name"])
        save_processed(weather)
    except WeatherFetchError as e:
        raise HTTPException(status_code=502,
                            detail=f"Could not call Open-Meteo.\
                                    Error Message: {e}")
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"Was able to call Open-Meteo,\
                                    but could not store the result.\
                                    Error Message: {e}")
    return {"station": selected["station_name"],
            "start": start,
            "end": end,
            "rows": len(weather),
            "rain_hours": int(weather["is_raining"].sum()),
            "snow_hours": int(weather["is_snowing"].sum()),
            "freezing_precip_hours": int(weather["is_freezing_precip"].sum()),
            "message": "Successfully stored the extracted data"}


@app.post("/weather/upload")
def upload_to_bucket(upload_input: UploadInput):
    if not upload_input.bucket:
        raise HTTPException(status_code=400,
                            detail="No bucket given. Set GCP_BUCKET_NAME in\
                                    .env or pass bucket in the request body.")

    files = [(database_path, f"{gcs_processed_prefix}/weather.db"),
             (parquet_path, f"{gcs_processed_prefix}/weather_hourly.parquet")]
    missing = [path for path, name in files if not os.path.exists(path)]
    if missing:
        raise HTTPException(status_code=409,
                            detail=f"Nothing to upload, missing: {missing}.")
    try:
        for path, name in files:
            upload_file_to_gcs(upload_input.bucket, name, path)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"Could not upload to\
                                    {upload_input.bucket}.\
                                    Error Message: {e}")
    return {"message": f"weather.db and weather_hourly.parquet have been "
                       f"uploaded to {upload_input.bucket} successfully."}


@app.post("/ridership/refresh")
def refresh():
    res = run(read_dates_file(EVENTS_PATH))
    gcs.save_csv(res["vs_baseline"], VS_FILE, VS_DATE_FORMAT)
    gcs.save_csv(res["hourly_all"], HOURLY_ALL_FILE, HOURLY_DATE_FORMAT)
    raw_files = save_raw(res["raw"])
    return {
        "event_dates": [str(d.date()) for d in res["event_dates"]],
        "dropped_dates": [str(d.date()) for d in res["dropped_dates"]],
        "baseline": [str(res["baseline_start"].date()), str(res["baseline_end"].date())],
        "files": [gcs.location(VS_FILE), gcs.location(HOURLY_ALL_FILE)],
        "raw_files": raw_files,
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


@app.get("/ridership/raw")
def raw(date: str, station: str | None = None, line: str | None = None):
    """Raw ArcGIS rows for one service date (3am to 3am), all stations, 30 minute periods."""
    try:
        day = pd.Timestamp(date)
    except ValueError:
        raise HTTPException(422, "date must look like YYYY-MM-DD")
    name = raw_file(day.strftime("%Y-%m"))
    try:
        df = gcs.read_csv(name, parse_dates=["service_date"], prefix=gcs.RAW_PREFIX)
    except FileNotFoundError:
        raise HTTPException(404, f"{gcs.location(name, gcs.RAW_PREFIX)} not found, "
                                 "POST /ridership/refresh first")
    df = df[df["service_date"] == day.normalize()]
    if station:
        s = station.lower()
        df = df[(df["stop_id"].str.lower() == s) | (df["station_name"].str.lower() == s)]
    if line:
        df = df[df["route_or_line"].str.contains(line, case=False, regex=False)]
    df["service_date"] = df["service_date"].dt.strftime(RAW_DATE_FORMAT)
    return json.loads(df.to_json(orient="records"))
