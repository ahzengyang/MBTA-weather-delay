""" 
fastapi/mbta_stations_api.py — api wrapper of the station_data.py script 

Endpoints:
GET  /health 
     (return status)
GET  /stations
     Runs full pipeline of data acquisition and transformation to build final 
     subway station dataset. 
POST /stations/upload 
     Uploads the final subway station to GCS 

Run directly:
    python station_api.py

Config info (URLs, GCS info) comes from user_definition.py.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from google.api_core.exceptions import GoogleAPIError

import station_data as stations
import user_definition as cfg

app = FastAPI(
    title="MBTA Station Info API",
    version="1.0",
    description="Subway station reference table (location, municipality, "
                "is_underground and more) built from MBTA's GTFS stops.txt and levels.txt.",
)

# Runs through data acquisition and transformation pipeline
def build_station_table():
    stops_df = stations.get_raw_csv(cfg.mbta_gtfs_url, "stops.txt")
    levels_df = stations.get_raw_csv(cfg.mbta_gtfs_url, "levels.txt")

    underground_flags = stations.get_underground_flags(stops_df, levels_df)
    processed_stops_df = stations.process_subway_stations(stops_df, underground_flags)
    return processed_stops_df

"""
Endpoint Functions
"""

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/stations")
def get_stations(
    fmt: Annotated[Literal["json", "csv"],Query(alias="format", description="json or csv")] = "json",
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
    = f"station_processed/{cfg.station_data_file_name}",
) -> dict:
    try:
        station_df = build_station_table()
    except Exception as exc:
        raise HTTPException(502, f"Could not build station table: {exc}")

    try:
        stations.upload_to_gcs(
            station_df.to_csv(index=False).encode("utf-8"),
            cfg.bucket_name,
            blob_path,
        )
    except GoogleAPIError as exc:
        raise HTTPException(502, f"Upload to gs://{cfg.bucket_name}/{blob_path} failed: {exc}")

    return {
        "status": "success",
        "n_stations": len(station_df),
        "uploaded": f"gs://{cfg.bucket_name}/{blob_path}",
    }
