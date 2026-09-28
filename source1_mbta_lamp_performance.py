"""
Source 1: MBTA LAMP subway on-time performance (historical file download)

Portal: https://performancedata.mbta.com/
Dictionary: https://github.com/mbta/lamp/blob/main/Data_Dictionary.md

One Parquet file per service date:
  https://performancedata.mbta.com/lamp/subway-on-time-performance-v1/YYYY-MM-DD-subway-on-time-performance-v1.parquet
An index of every published date lives at .../index.csv

Each row is one trip_id-stop_id pair: a single train's visit to a single
platform, with the scheduled time and the observed time.

THE UNIT TRAP (read this before trusting any delay number)
    scheduled_arrival_time / scheduled_departure_time
        int64, SECONDS AFTER MIDNIGHT of service_date, local service day.
        Values can exceed 86400 for after-midnight service.
    stop_timestamp / move_timestamp
        int64, UNIX EPOCH SECONDS (UTC).
    These are different clocks in different units. Subtracting them raw gives
    nonsense. `add_delay_columns` converts the schedule to epoch first.

OTHER GOTCHAS
    - `parent_station` holds the station NAME, not an ID. There is no
      stop_name column. This is the field you join to Wikipedia / gated entries.
    - `stop_timestamp` is the observed "STOPPED_AT" time, but falls back to the
      last predicted arrival from StopTimeUpdate when the vehicle feed is
      missing -- and there is no flag saying which was used.
    - There is no actual-departure column. Departure from stop N is the
      `move_timestamp` of stop N+1, or `stop_timestamp + dwell_time_seconds`.
    - `branch_route_id` is NULL for single-route lines (Blue, Orange). LAMP
      inserts Red-A / Red-B for Red Line trips south of JFK/UMass.
"""

from __future__ import annotations

import io

import pandas as pd
import requests

BASE = "https://performancedata.mbta.com/lamp/subway-on-time-performance-v1"
INDEX_URL = f"{BASE}/index.csv"
SERVICE_TZ = "America/New_York"

HEADERS = {"User-Agent": "transit-delay-weather-class-project/1.0"}


def list_service_dates() -> pd.DataFrame:
    """Every published service date. Use this to pick a real date range."""
    resp = requests.get(INDEX_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()

    df = pd.read_csv(io.BytesIO(resp.content))
    df.columns = [c.strip().lower() for c in df.columns]

    date_col = next((c for c in df.columns if "date" in c), df.columns[0])
    df["service_date"] = pd.to_datetime(df[date_col], errors="coerce")
    return df.dropna(subset=["service_date"]).sort_values("service_date")


def fetch_service_date(date: str) -> pd.DataFrame:
    """One service date. `date` is 'YYYY-MM-DD'."""
    url = f"{BASE}/{date}-subway-on-time-performance-v1.parquet"
    resp = requests.get(url, headers=HEADERS, timeout=180)
    resp.raise_for_status()
    return pd.read_parquet(io.BytesIO(resp.content))


def add_delay_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Put schedule and observation on the same clock, then difference them.

    Adds:
        service_day          midnight of the service date (tz-aware, local)
        scheduled_arrival    tz-aware local timestamp
        actual_arrival       tz-aware local timestamp
        arrival_delay_min    actual - scheduled, in minutes (+ = late)
        arrival_hour_local   hour of the local service day, for the weather join

    DST caveat: GTFS seconds-after-midnight are defined against "noon minus
    12 hours", not against literal local midnight. On the two DST changeover
    days per year, times after the transition shift by an hour. Negligible for
    most analysis; drop those two dates if you want to be strict.
    """
    out = df.copy()

    # service_date arrives as int64 YYYYMMDD.
    service_day = pd.to_datetime(
        out["service_date"].astype("int64").astype(str), format="%Y%m%d"
    ).dt.tz_localize(SERVICE_TZ, nonexistent="shift_forward", ambiguous="NaT")
    out["service_day"] = service_day

    for src, name in [
        ("scheduled_arrival_time", "scheduled_arrival"),
        ("scheduled_departure_time", "scheduled_departure"),
    ]:
        if src in out.columns:
            out[name] = service_day + pd.to_timedelta(out[src], unit="s")

    # Observed times are plain unix epoch seconds.
    for src, name in [
        ("stop_timestamp", "actual_arrival"),
        ("move_timestamp", "approach_time"),
    ]:
        if src in out.columns:
            out[name] = (
                pd.to_datetime(out[src], unit="s", utc=True, errors="coerce")
                .dt.tz_convert(SERVICE_TZ)
            )

    out["arrival_delay_min"] = (
        out["actual_arrival"] - out["scheduled_arrival"]
    ).dt.total_seconds() / 60

    out["arrival_hour_local"] = out["actual_arrival"].dt.floor("h")

    return out


def fetch_dates(dates: list[str], with_delay: bool = True) -> pd.DataFrame:
    """Stack several service dates into one DataFrame."""
    frames = []
    for date in dates:
        part = fetch_service_date(date)
        frames.append(add_delay_columns(part) if with_delay else part)
        print(f"{date}: {len(frames[-1]):,} rows")
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    index = list_service_dates()
    print(f"published dates: {len(index):,}")
    print(f"range: {index['service_date'].min().date()} .. {index['service_date'].max().date()}")

    # Grab a recent date that definitely exists rather than guessing one.
    latest = index["service_date"].iloc[-2].strftime("%Y-%m-%d")
    df = fetch_dates([latest])

    print(f"\nshape: {df.shape}")
    print(
        df[
            [
                "parent_station",
                "trunk_route_id",
                "direction_destination",
                "scheduled_arrival",
                "actual_arrival",
                "arrival_delay_min",
            ]
        ]
        .head(15)
        .to_string()
    )
    print(f"\nmedian delay (min): {df['arrival_delay_min'].median():.2f}")
    print(f"missing actual_arrival: {df['actual_arrival'].isna().sum():,} / {len(df):,}")
    print(f"stations: {df['parent_station'].nunique()}")
