"""HTTP interface to the Open-Meteo weather collector.

Two operations, matching the project's two halves:

    GET  /weather?station=Alewife    fetch one station from Open-Meteo and store it
    POST /bucket/upload              push the stored database and Parquet to GCS

The batch pipeline is unchanged and remains how the full dataset is built --
`python fillweather.py` collects all 125 stations in about six minutes, which is
far too long to hold an HTTP connection open. This serves single-station fetches
and the upload step.

    pip install "fastapi[standard]" google-cloud-storage
    fastapi run api.py

Both routes call the same collection code the batch script uses, so the two
cannot drift apart.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from google.cloud import storage

from weather_openmeteo import (
    DB_PATH, PROCESSED_DIR, WeatherFetchError, fetch_weather, log, save_processed,
)

BUCKET = "mbta-weather-delay"
STATIONS_CSV = Path("stations.csv")
DEFAULT_START = "2025-06-01"
# A full station-range fetch is ~11,700 rows. Returning all of them as JSON is
# megabytes per request, so the response carries a summary plus a sample.
MAX_SAMPLE_ROWS = 24

app = FastAPI(
    title="MBTA Weather Collector",
    description="Source 2 of the MBTA weather-delay project.",
    version="1.0.0",
)


def _stations() -> pd.DataFrame:
    if not STATIONS_CSV.exists():
        raise HTTPException(
            status_code=500,
            detail=f"{STATIONS_CSV} not found -- run `python get_stations.py` first",
        )
    return pd.read_csv(STATIONS_CSV)


@app.get("/health")
def health() -> dict:
    """Liveness check, plus whether a collected database exists yet."""
    return {
        "status": "ok",
        "database_present": DB_PATH.exists(),
        "database_mb": round(DB_PATH.stat().st_size / 1e6, 1) if DB_PATH.exists() else 0,
    }


@app.get("/stations")
def list_stations() -> dict:
    """The 125 subway parent stations the collector knows about."""
    df = _stations()
    return {
        "count": len(df),
        "stations": df[["station_id", "station_name", "latitude",
                        "longitude", "lines"]].to_dict(orient="records"),
    }


@app.get("/weather")
def get_weather(
    station: str = Query(..., description="Station name, e.g. Alewife"),
    start: str = Query(DEFAULT_START, description="YYYY-MM-DD"),
    end: str | None = Query(None, description="YYYY-MM-DD, defaults to yesterday"),
    store: bool = Query(True, description="also write the rows into weather.db"),
) -> dict:
    """Fetch one station's hourly weather from Open-Meteo.

    Returns a summary plus a sample of rows. The full result is written to the
    database when `store` is true, which is where the analysis reads it from.
    """
    end = end or (date.today() - timedelta(days=1)).isoformat()

    df = _stations()
    match = df[df["station_name"].str.lower() == station.lower()]
    if match.empty:
        raise HTTPException(
            status_code=404,
            detail=f"unknown station {station!r} -- see GET /stations",
        )
    st = match.iloc[0]

    try:
        rows = fetch_weather(
            st["latitude"], st["longitude"], start, end,
            include_visibility=True,          # -> historical-forecast, ~2 km grid
        )
    except WeatherFetchError as exc:
        # Upstream failed after its retries -- that is a gateway problem, not ours.
        raise HTTPException(status_code=502, detail=f"Open-Meteo: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows.insert(0, "location", st["station_name"])
    if store:
        save_processed(rows, write_csv=False)

    sample = rows.head(MAX_SAMPLE_ROWS).copy()
    for col in ("observed_at_utc", "observed_at_local", "ingested_at_utc"):
        if col in sample.columns:
            sample[col] = sample[col].astype(str)

    return {
        "station": st["station_name"],
        "requested_coordinates": [float(st["latitude"]), float(st["longitude"])],
        # Open-Meteo snaps to its grid -- this is the cell the data is actually from.
        "grid_coordinates": [float(rows["latitude"].iloc[0]),
                             float(rows["longitude"].iloc[0])],
        "start": start,
        "end": end,
        "rows": len(rows),
        "stored": bool(store),
        "rain_hours": int(rows["is_raining"].sum()),
        "snow_hours": int(rows["is_snowing"].sum()),
        "freezing_precip_hours": int(rows["is_freezing_precip"].sum()),
        "sample": sample.to_dict(orient="records"),
    }


@app.post("/bucket/upload")
def upload_to_bucket(
    bucket: str = Query(BUCKET, description="GCS bucket name"),
) -> dict:
    """Push the stored database and Parquet export to Cloud Storage.

    Uses Application Default Credentials -- whatever `gcloud auth` is logged in
    as. The database is a few hundred megabytes, so this call takes tens of
    seconds rather than returning instantly.
    """
    parquet = PROCESSED_DIR / "weather_hourly.parquet"
    targets = [(DB_PATH, "processed/weather.db"),
               (parquet, "processed/weather_hourly.parquet")]

    missing = [str(p) for p, _ in targets if not p.exists()]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"nothing to upload, missing: {missing} -- run the collector first",
        )

    try:
        client = storage.Client()
        blob_bucket = client.bucket(bucket)
        uploaded = []
        for path, key in targets:
            blob = blob_bucket.blob(key)
            blob.upload_from_filename(str(path))
            uploaded.append({
                "source": str(path),
                "destination": f"gs://{bucket}/{key}",
                "megabytes": round(path.stat().st_size / 1e6, 1),
            })
            log.info("uploaded %s -> gs://%s/%s", path, bucket, key)
    except Exception as exc:
        # Credentials, permissions and network all land here; surface the reason
        # rather than a bare 500.
        raise HTTPException(status_code=502, detail=f"GCS upload failed: {exc}") from exc

    return {"bucket": bucket, "uploaded": uploaded}
