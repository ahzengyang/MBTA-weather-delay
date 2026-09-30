"""
Source 2: Open-Meteo historical hourly weather for Boston (API, no key needed)

Two endpoints, because they trade off differently:

  ARCHIVE (default)  https://archive-api.open-meteo.com/v1/archive
      ERA5 reanalysis, 1940 -> ~yesterday. Long history, but a coarse ~25 km
      grid and NO `visibility` variable (ERA5 doesn't model it).

  HISTORICAL FORECAST https://historical-forecast-api.open-meteo.com/v1/forecast
      Archived high-resolution forecast runs, 2022 -> today. ~2 km grid and
      includes `visibility`. Use this if you want visibility or finer detail.

Both take start_date / end_date, so unlike the live forecast endpoint you can
request exactly the dates your delay data covers.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pandas as pd
import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

LOCAL_TZ = "America/New_York"

# We ALWAYS ask the API for UTC and convert to local ourselves. Open-Meteo's
# `timezone` parameter applies a single fixed UTC offset to the whole request
# instead of real DST rules, which silently shifts every EST-period row by an
# hour. UTC has no ambiguous or nonexistent hours, so converting is exact.
API_TIMEZONE = "GMT"

# Available on both endpoints.
CORE_VARS = [
    "temperature_2m",
    "precipitation",
    "rain",
    "snowfall",
    "wind_speed_10m",
    "wind_gusts_10m",
    "weather_code",
    "cloud_cover",
]
# Only on the historical-forecast endpoint.
FORECAST_ONLY_VARS = ["visibility"]

# Enough spread to distinguish tunnel from surface running. Swap in the real
# coordinates that source 4 scrapes from each station's Wikipedia infobox.
SAMPLE_LOCATIONS = {
    "Downtown Boston": (42.3554, -71.0605),   # Park St / tunnel core
    "Boston College": (42.3400, -71.1665),    # Green Line B, surface
    "Braintree": (42.2078, -71.0011),         # Red Line, surface
    "Wonderland": (42.4131, -70.9919),        # Blue Line, surface + coastal
}

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "raw" / "weather"
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = PROCESSED_DIR / "weather.db"
LOG_PATH = Path("logs") / "weather.log"

MAX_RETRIES = 5
TIMEOUT_SECONDS = 60
# Retry these; anything else (notably 400) is a permanent error and retrying
# it just burns quota for the same failure.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

for _d in (RAW_DIR, PROCESSED_DIR, LOG_PATH.parent):
    _d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("weather")


class WeatherFetchError(RuntimeError):
    """Raised when a request fails permanently or exhausts its retries."""


def _get_with_retry(url: str, params: dict) -> dict:
    """GET with exponential backoff + jitter. Honours Retry-After on 429."""
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)

            if resp.status_code == 200:
                data = resp.json()
                if "hourly" not in data:
                    raise WeatherFetchError(f"unexpected response: {data}")
                return data

            # Open-Meteo returns {"error": true, "reason": "..."} on failure.
            try:
                reason = resp.json().get("reason", resp.text[:200])
            except ValueError:
                reason = resp.text[:200]

            if resp.status_code not in RETRYABLE_STATUS:
                raise WeatherFetchError(f"HTTP {resp.status_code}: {reason}")

            wait = float(resp.headers.get("Retry-After", 0)) or _backoff(attempt)
            log.warning("HTTP %s (%s), attempt %d/%d; retrying in %.1fs",
                        resp.status_code, reason, attempt, MAX_RETRIES, wait)
            last_error = WeatherFetchError(f"HTTP {resp.status_code}: {reason}")
            time.sleep(wait)

        except (requests.Timeout, requests.ConnectionError) as exc:
            wait = _backoff(attempt)
            log.warning("%s, attempt %d/%d; retrying in %.1fs",
                        type(exc).__name__, attempt, MAX_RETRIES, wait)
            last_error = exc
            time.sleep(wait)

    raise WeatherFetchError(
        f"gave up after {MAX_RETRIES} attempts: {url} {params}"
    ) from last_error


def _backoff(attempt: int) -> float:
    """2, 4, 8, 16... seconds, capped, plus jitter."""
    return min(2 ** attempt, 60) + random.uniform(0, 1)


def _save_raw(data: dict, params: dict, url: str) -> Path:
    """Persist the untouched response so we can re-parse without re-fetching."""
    stamp = datetime.now(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = (f"{data['latitude']}_{data['longitude']}_"
            f"{params['start_date']}_{params['end_date']}_{stamp}.json.gz")
    path = RAW_DIR / name
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"fetched_at_utc": datetime.now(dt_timezone.utc).isoformat(),
                   "url": url, "params": params, "response": data}, fh)
    return path


def fetch_weather(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
    include_visibility: bool = False,
    local_tz: str = LOCAL_TZ,
) -> pd.DataFrame:
    """Hourly weather for one point over a date range (dates are 'YYYY-MM-DD')."""
    hourly = list(CORE_VARS)
    url = ARCHIVE_URL
    if include_visibility:
        url = HISTORICAL_FORECAST_URL
        hourly += FORECAST_ONLY_VARS

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": ",".join(hourly),
        "timezone": API_TIMEZONE,
    }
    log.info("fetch (%.4f, %.4f) %s..%s", latitude, longitude, start_date, end_date)
    data = _get_with_retry(url, params)
    _save_raw(data, params, url)

    df = pd.DataFrame(data["hourly"]).rename(columns={"time": "observed_at_utc"})

    # Returned as naive GMT strings. Label them UTC, then convert -- tz_convert
    # applies real DST rules, so nothing is dropped or mislabelled.
    df["observed_at_utc"] = pd.to_datetime(df["observed_at_utc"], utc=True)
    df["observed_at_local"] = df["observed_at_utc"].dt.tz_convert(local_tz)

    # These are the coordinates the API SNAPPED to, which may differ from the
    # ones we asked for -- they are the real identity of the observation.
    df["latitude"] = data["latitude"]
    df["longitude"] = data["longitude"]

    # `precipitation` is rain + snow water equivalent, so keying is_raining on
    # it labels every snowstorm as rain. Use `rain` for rain specifically.
    zeros = pd.Series(0.0, index=df.index)
    df["is_precipitating"] = df.get("precipitation", zeros).fillna(0) > 0
    df["is_raining"] = df.get("rain", zeros).fillna(0) > 0
    df["is_snowing"] = df.get("snowfall", zeros).fillna(0) > 0

    _validate(df, hourly, start_date, end_date)
    return df


def _validate(df: pd.DataFrame, requested: list[str], start: str, end: str) -> None:
    """Log anything suspicious rather than silently shipping bad data."""
    expected = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    if len(df) != expected * 24:
        log.warning("expected %d hourly rows, got %d", expected * 24, len(df))
    for col in requested:
        if col in df.columns and df[col].isna().all():
            log.error("column %r is entirely null -- unsupported on this endpoint?", col)
    if df["observed_at_utc"].duplicated().any():
        log.warning("duplicate timestamps present")


def fetch_weather_for_locations(
    start_date: str,
    end_date: str,
    locations: dict[str, tuple[float, float]] | None = None,
    **kwargs,
) -> pd.DataFrame:
    """Stack hourly weather for several named points into one long DataFrame."""
    locations = locations or SAMPLE_LOCATIONS
    frames, failed = [], []
    for name, (lat, lon) in locations.items():
        try:
            part = fetch_weather(lat, lon, start_date, end_date, **kwargs)
        except WeatherFetchError:
            # One bad location must not kill the whole run.
            log.exception("%s failed; continuing", name)
            failed.append(name)
            continue
        part.insert(0, "location", name)
        frames.append(part)

    if not frames:
        raise WeatherFetchError("every location failed; nothing collected")
    if failed:
        log.error("%d of %d locations failed: %s", len(failed), len(locations), failed)
    return pd.concat(frames, ignore_index=True)


# Pinned table schema. Building it from whatever columns the first DataFrame
# happened to have meant an archive run (no visibility) froze a schema that a
# later historical-forecast run could not write into.
TABLE_COLUMNS = (
    ["location", "observed_at_utc", "observed_at_local", "latitude", "longitude"]
    + CORE_VARS
    + FORECAST_ONLY_VARS
    + ["is_precipitating", "is_raining", "is_snowing", "ingested_at_utc"]
)


def save_processed(df: pd.DataFrame, db_path: Path = DB_PATH,
                   write_csv: bool = True) -> None:
    """Write to SQLite. The primary key makes re-running a range idempotent."""
    out = df.copy()
    if "location" not in out.columns:
        out["location"] = out["latitude"].astype(str) + "," + out["longitude"].astype(str)
    out["ingested_at_utc"] = datetime.now(dt_timezone.utc).isoformat()
    for c in ("observed_at_utc", "observed_at_local"):
        out[c] = out[c].apply(lambda v: v.isoformat() if pd.notna(v) else None)
    for c in ("is_precipitating", "is_raining", "is_snowing"):
        out[c] = out[c].astype(int)

    # Columns absent on this endpoint (e.g. visibility on archive) store NULL.
    out = out.reindex(columns=TABLE_COLUMNS)

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cols_sql = ", ".join(f'"{c}"' for c in TABLE_COLUMNS)
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS weather_hourly ({cols_sql}, '
            f'PRIMARY KEY (location, observed_at_utc))'
        )
        placeholders = ",".join("?" * len(TABLE_COLUMNS))
        conn.executemany(
            f"INSERT OR REPLACE INTO weather_hourly ({cols_sql}) VALUES ({placeholders})",
            [tuple(None if pd.isna(v) else v for v in r)
             for r in out.itertuples(index=False)],
        )
        conn.commit()
        log.info("upserted %d rows into %s", len(out), db_path)
    finally:
        conn.close()

    if write_csv:
        csv_path = PROCESSED_DIR / "weather_hourly.csv"
        df.to_csv(csv_path, index=False)
        log.info("wrote %s", csv_path)


if __name__ == "__main__":
    df = fetch_weather_for_locations("2026-01-01", "2026-01-07")
    save_processed(df)
    print(f"shape: {df.shape}")
    print(df.head(10).to_string())
    print(f"\nrainy hours:  {df['is_raining'].sum()} / {len(df)}")
    print(f"snowy hours:  {df['is_snowing'].sum()} / {len(df)}")
