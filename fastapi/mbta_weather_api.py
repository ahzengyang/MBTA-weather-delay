"""A combined file for all four of the team's API data sources endpoints (station_api.py, mbta_lamp_api.py, 
fastapi_weather.py, and api.py in MBTA delay data

To run, use command: fastapi run mbta_weather_api.py

Make sure to have the following files ready: user_definition.py, station_data.py, source1_mbta_lamp_delay.py, weather_openmeteo.py, run_pipeline.py, 
gcs.py, gse_fetch.py, gse_clean.py, stations.py
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from google.api_core.exceptions import GoogleAPIError

import combine_sources as combine

import fastapi_functions.gcs as gcs
import fastapi_functions.source1_mbta_lamp_delay as lamp
import fastapi_functions.station_data as station_data
from fastapi_functions.run_pipeline import (HOURLY_DATE_FORMAT, HOURLY_FILE, RAW_DATE_FORMAT,
                          raw_file, run, save_raw)
from fastapi_functions.weather_openmeteo import (WeatherFetchError, fetch_weather,
                               save_processed)
from fastapi_functions.user_definition import *

MAX_RANGE_DAYS = int(os.getenv("MBTA_API_MAX_RANGE_DAYS", "500"))
CACHE_DAYS = int(os.getenv("MBTA_API_CACHE_DAYS", "62"))
MIN_AGE_DAYS = 2

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
    = f"{station_gcs_prefix}/{station_data_file_name}",
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


def load_stations() -> pd.DataFrame:
    """Station table uploaded by POST /stations/upload, with the column names
    the weather endpoints use."""
    try:
        stations = gcs.read_csv(station_data_file_name, parse_dates=None,
                                prefix=station_gcs_prefix)
    except FileNotFoundError:
        raise HTTPException(404, f"{gcs.location(station_data_file_name, station_gcs_prefix)} "
                                 "not found, POST /stations/upload first")
    return stations.rename(columns={"stop_name": "station_name",
                                    "stop_lat": "latitude",
                                    "stop_lon": "longitude"})


@app.get("/weather")
def get_weather(station: str,
                start: str = start_date,
                end: str = end_date):
    stations = load_stations()
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
        stored = save_processed(weather,
                                f"{selected['stop_id']}_{start}_{end}.parquet")
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
            "stored": stored,
            "message": "Successfully stored the extracted data"}


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


def daily_frame(target: str) -> pd.DataFrame:
    """Every station for one service date. Each station's raw response is
    uploaded to GCS by fetch_weather()."""
    stations = load_stations()
    frames = []
    for station in stations.itertuples(index=False):
        weather = fetch_weather(station.latitude,
                                station.longitude,
                                target,
                                target,
                                include_visibility=True)
        weather.insert(0, "location", station.station_name)
        frames.append(weather)
    return pd.concat(frames, ignore_index=True)


@app.post("/weather/refresh")
def refresh_weather(start: str, end: str | None = None):
    #Hourly weather for every station from the START to END (END by default is today)
    today = datetime.now(ZoneInfo(local_timezone)).date()
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end) if end else today
    except ValueError:
        raise HTTPException(422, "start and end must look like YYYY-MM-DD")
    end_day = min(end_day, today)
    if start_day > end_day:
        raise HTTPException(400, f"start ({start_day}) is after end ({end_day}).")

    stations = load_stations()
    frames, failed = [], []
    for station in stations.itertuples(index=False):
        try:
            weather = fetch_weather(station.latitude,
                                    station.longitude,
                                    start_day.isoformat(),
                                    end_day.isoformat(),
                                    include_visibility=True)
        except (WeatherFetchError, GoogleAPIError, ValueError) as e:
            failed.append({"station": station.station_name, "reason": str(e)})
            continue
        weather.insert(0, "location", station.station_name)
        frames.append(weather)

    if not frames:
        raise HTTPException(502, {"message": "Could not fetch weather for any station.",
                                  "failed": failed})

    data = pd.concat(frames, ignore_index=True)
    try:
        processed = save_processed(data,
                                   f"weather_hourly.parquet")
    except GoogleAPIError as e:
        raise HTTPException(502, f"Could not upload to GCS. Error Message: {e}")

    return {"start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "stations": int(data["location"].nunique()),
            "stations_failed": failed,
            "rows": len(data),
            "rain_hours": int(data["is_raining"].sum()),
            "snow_hours": int(data["is_snowing"].sum()),
            "freezing_precip_hours": int(data["is_freezing_precip"].sum()),
            "processed": processed,
            "raw": f"gs://{gcs.BUCKET}/{gcs_raw_prefix}/",
            "message": "Successfully stored the extracted data"}


@app.post("/weather/daily")
def collect_daily(day: str | None = None):
    """Collect one day for every station and write it straight to GCS."""
    target = day or (datetime.now(timezone.utc).date()
                     - timedelta(days=1)).isoformat()
    try:
        data = daily_frame(target)
        processed = save_processed(data, f"daily/weather_{target}.parquet")
    except (WeatherFetchError, ValueError) as e:
        raise HTTPException(status_code=502,
                            detail=f"Could not collect {target}. Error Message: {e}")
    except GoogleAPIError as e:
        raise HTTPException(status_code=502,
                            detail=f"Could not upload {target} to GCS. Error Message: {e}")

    return {"date": target,
            "stations": int(data["location"].nunique()),
            "rows": len(data),
            "rain_hours": int(data["is_raining"].sum()),
            "snow_hours": int(data["is_snowing"].sum()),
            "freezing_precip_hours": int(data["is_freezing_precip"].sum()),
            "processed": processed,
            "raw": f"gs://{gcs.BUCKET}/{gcs_raw_prefix}/",
            "message": "Successfully stored the extracted data"}


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


@app.post("/combined/refresh")
def refresh_combined(
    station_blob: Annotated[str, Query(description="Station table inside the bucket")]
    = f"{station_gcs_prefix}/{station_data_file_name}",
    output_blob: Annotated[str, Query(description="Where to write the combined CSV")]
    = combine.COMBINED_BLOB,
) -> dict:
    """Join the latest delay panel with station, ridership and weather data from
    GCS, and write the combined table back to GCS."""
    try:
        lamp.check_gcp_config()
        bucket = lamp.get_bucket()
    except (RuntimeError, FileNotFoundError, ValueError, GoogleAPIError) as exc:
        raise HTTPException(500, f"GCP setup failed: {exc}")

    sources = {}
    try:
        delays, stations, weather, ridership = combine.load_sources_from_gcs(
            bucket, station_blob=station_blob, sources=sources)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Missing input in GCS: {exc}")
    except GoogleAPIError as exc:
        raise HTTPException(502, f"Could not read from GCS: {exc}")

    try:
        combined = combine.build_combined(delays, stations, weather, ridership)
    except (AssertionError, KeyError, ValueError) as exc:
        raise HTTPException(500, f"Could not combine the sources: "
                                 f"{type(exc).__name__}: {exc}")

    try:
        combine.upload_csv(bucket, combined, output_blob)
    except GoogleAPIError as exc:
        raise HTTPException(502, f"Upload to gs://{bucket.name}/{output_blob} failed: {exc}")

    def match_rate(column):
        return round(float(combined[column].notna().mean()), 4) if len(combined) else 0.0

    max_km = combined["weather_distance_km"].max()
    return {
        "sources": {label: f"gs://{bucket.name}/{name}" for label, name in sources.items()},
        "rows": len(combined),
        "columns": len(combined.columns),
        "hour_utc_first": str(combined["hour_utc"].min()),
        "hour_utc_last": str(combined["hour_utc"].max()),
        "match_rate": {"station": match_rate("station_stop_name"),
                       "ridership": match_rate("hourly_entries"),
                       "weather": match_rate("weather_location")},
        "weather_distance_km_max": None if pd.isna(max_km) else round(float(max_km), 2),
        "uploaded": f"gs://{bucket.name}/{output_blob}",
    }