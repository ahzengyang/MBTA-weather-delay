"""
Open-Meteo historical hourly weather for Boston (API)

Two endpoints:

  Archive (default)  https://archive-api.open-meteo.com/v1/archive
      1940 to ~ yesterday. On roughly a ~25 km grid with no visibility variables.

  Historical Forceast https://historical-forecast-api.open-meteo.com/v1/forecast
      2022 to today. On roughly a ~2 km grid and includes visibility. 

Both take start_date and end_date, so we can request exactly the dates our delay 
data covers.
"""
import gzip
import io
import json
import random
import time
from datetime import datetime, timezone
import pandas as pd
import requests
from . import gcs
from .user_definition import *

class WeatherFetchError(Exception):
    """Raised when a request fails."""


def backoff(attempt: int) -> float:
    return min(2 ** attempt, 60) + random.uniform(0, 1)

def get_with_retry(url: str,
                   params: dict) -> dict:
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url,
                                    params=params,
                                    timeout=timeout_seconds)
            if response.status_code == 200:
                data = response.json()
                if "hourly" not in data:
                    raise WeatherFetchError(f"unexpected response: {data}")
                return data

            try:
                reason = response.json().get("reason", response.text[:200])
            except ValueError:
                reason = response.text[:200]

            if response.status_code not in retryable_status:
                raise WeatherFetchError(
                    f"HTTP {response.status_code}: {reason}")

            wait = (float(response.headers.get("Retry-After", 0))
                    or backoff(attempt))
            print(f"HTTP {response.status_code} ({reason}), "
                  f"attempt {attempt}/{max_retries}, retrying in {wait:.1f}s")
            last_error = WeatherFetchError(
                f"HTTP {response.status_code}: {reason}")
            time.sleep(wait)

        except (requests.Timeout, requests.ConnectionError) as e:
            wait = backoff(attempt)
            print(f"{type(e).__name__}, "
                  f"attempt {attempt}/{max_retries}, retrying in {wait:.1f}s")
            last_error = e
            time.sleep(wait)

    raise WeatherFetchError(f"gave up after {max_retries} attempts: "
                            f"{url} {params}") from last_error

def save_raw(data: dict,
             params: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_name = (f"{data['latitude']}_{data['longitude']}_"
                 f"{params['start_date']}_{params['end_date']}_"
                 f"{stamp}.json.gz")
    payload = json.dumps({"fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                          "params": params,
                          "response": data}).encode("utf-8")
    return gcs.save_bytes(gzip.compress(payload), file_name,
                          "application/gzip", prefix=gcs_raw_prefix)

def validate_data(data: pd.DataFrame,
                  requested: list,
                  start: str,
                  end: str) -> None:
    expected = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    if len(data) != expected * 24:
        print(f"expected {expected * 24} hourly rows, got {len(data)}")
    for column in requested:
        if column in data.columns and data[column].isna().all():
            print(f"column {column} is entirely null")
    if data["observed_at_utc"].duplicated().any():
        print("duplicate timestamps present")

def fetch_weather(latitude: float,
                  longitude: float,
                  start: str,
                  end: str,
                  include_visibility: bool = False) -> pd.DataFrame:
    hourly = list(core_variables)
    url = archive_url
    if include_visibility:
        url = historical_forecast_url
        hourly = hourly + forecast_only_variables

    params = {"latitude": latitude,
              "longitude": longitude,
              "start_date": start,
              "end_date": end,
              "hourly": ",".join(hourly),
              "timezone": api_timezone}

    print(f"fetch ({latitude:.4f}, {longitude:.4f}) {start}..{end}")
    data = get_with_retry(url, params)
    save_raw(data, params)

    weather = pd.DataFrame(data["hourly"]).rename(
        columns={"time": "observed_at_utc"})
    weather["observed_at_utc"] = pd.to_datetime(weather["observed_at_utc"],
                                                utc=True)
    weather["observed_at_local"] = weather["observed_at_utc"].dt.tz_convert(
        local_timezone)
    weather["latitude"] = data["latitude"]
    weather["longitude"] = data["longitude"]

    zeros = pd.Series(0.0, index=weather.index)
    wet = weather.get("rain", zeros).fillna(0) > 0
    temperature = weather.get("temperature_2m", zeros).fillna(0)
    weather["is_raining"] = wet & (temperature > 0)
    weather["is_freezing_precip"] = wet & (temperature <= 0)
    weather["is_snowing"] = weather.get("snowfall", zeros).fillna(0) > 0
    weather["is_precipitating"] = wet | weather["is_snowing"]

    validate_data(weather, hourly, start, end)
    return weather

def save_processed(data: pd.DataFrame,
                   name: str) -> str:
    output = data.copy()
    output["ingested_at_utc"] = datetime.now(timezone.utc).isoformat()
    for column in ("observed_at_utc", "observed_at_local"):
        output[column] = output[column].apply(
            lambda value: value.isoformat() if pd.notna(value) else None)
    for column in flag_columns:
        output[column] = output[column].astype(int)
    output = output.reindex(columns=table_columns)

    buffer = io.BytesIO()
    output.to_parquet(buffer, index=False)
    location = gcs.save_bytes(buffer.getvalue(), name,
                              "application/vnd.apache.parquet",
                              prefix=gcs_processed_prefix)
    print(f"saved {len(output)} rows to {location}")
    return location
