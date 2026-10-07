"""An API script for the Open-Meteo site to collect weather.

It needed a get and a post:
    The get: GET  /weather?station=Alewife    fetch a station from the Open-Meteo site using 
                                              latitude and longitude and store it
    The post: POST /bucket/upload             send the stored stations as a database to GCS.
                                              Due to size, we had to use a parquet.
                                              
`python fillweather.py` collects all 125 subway stations, but that was too big for one request.
So it pulls one station at a time.

How to run:
    pip install "fastapi[standard]" google-cloud-storage
    fastapi run fastapi_weather.py

The routes share the same same cod eto stay in sync.
"""
import os
import pandas as pd
from fastapi import FastAPI, HTTPException
from google.cloud import storage
from pydantic import BaseModel
from user_definition import *
from weather_openmeteo import (WeatherFetchError, fetch_weather,
                               save_processed)

app = FastAPI()

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

@app.post("/bucket/upload")
def upload_to_bucket(upload_input: UploadInput):
    if not upload_input.bucket:
        raise HTTPException(status_code=400,
                            detail="No bucket given. Set GCP_BUCKET_NAME in\
                                    .env or pass bucket in the request body.")

    files = [(database_path, "processed/weather.db"),
             (parquet_path, "processed/weather_hourly.parquet")]
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
