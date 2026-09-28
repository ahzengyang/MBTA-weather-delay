"""
Source 3: MBTA Gated Station Entries (ArcGIS FeatureServer query)

Dataset: https://gis.data.mass.gov/datasets/MassDOT::mbta-gated-station-entries
Service: https://services1.arcgis.com/ceiitspzDAHrdGO1/arcgis/rest/services/GSE/FeatureServer/0

One row per (service_date, 30-minute period, station, line). ~2.07M rows.

COVERAGE: this service is the ROLLING ~2-YEAR window, not the full history.
As of this writing it runs 2024-08-25 .. 2026-06-30 -- note it lags the present
by a few months. Call `list_service_dates()` to see the live range instead of
guessing. For dates before that window, use the separate "MBTA Gated Station
Entries - Historical" item (2014->), which is published as per-year CSVs:
https://gis.data.mass.gov/datasets/7859894afb5641ce91a2bb03599fdf5b

Thirty-minute resolution is finer than the hourly weather, so aggregate up to
the hour before joining -- see `to_hourly`.

FIELD QUIRKS (all three of these bite; verified against the live service)
  - `time_period` is PARENTHESIZED: "(08:30:00)", not "08:30:00".
  - Only 48 distinct values exist, (00:00:00) .. (23:30:00). There is no
    (24:00:00) / (25:00:00), so after-midnight service is NOT encoded by
    hours >= 24. See the service-day note below.
  - `gated_entries` is a STRING, and is fractional at split stations.
  - `service_date` is epoch milliseconds at LOCAL midnight (04:00 or 05:00 UTC
    depending on DST), so it must be normalized to a calendar date.
  - In the `where` clause, date literals must be `timestamp '...'`.
    `DATE '...'` is accepted but silently matches nothing.

SERVICE DAY: runs 03:00 to 02:59 the next day. Since time_period only spans
00:00-23:30, the bins from (00:00:00) to (02:30:00) on service_date D are
actually the small hours of calendar day D+1. `fetch_gated_entries` applies
that rollover when building `period_start`.

OTHER CAVEATS the publisher calls out:
  - Entries are unscaled faregate taps: no free passengers, no fare evasion,
    employee taps included. Not total ridership.
  - Split stations (e.g. Government Center serves Blue + Green) are apportioned
    by CTPS survey factors, so they appear as TWO rows with fractional counts.
    Sum across route_or_line to get one figure per station.
  - Only GATED stations. Green Line surface stops have no faregates and are
    absent entirely, which matters since those are exactly the exposed stations.
"""

from __future__ import annotations

import pandas as pd
import requests

SERVICE_URL = (
    "https://services1.arcgis.com/ceiitspzDAHrdGO1"
    "/arcgis/rest/services/GSE/FeatureServer/0/query"
)
PAGE_SIZE = 2000  # the service's maxRecordCount
LOCAL_TZ = "America/New_York"

# A service day starts at 03:00, so bins before this hour belong to D+1.
SERVICE_DAY_START_HOUR = 3


def list_service_dates() -> pd.Series:
    """Every service date the rolling window currently holds."""
    resp = requests.get(
        SERVICE_URL,
        params={
            "where": "1=1",
            "outFields": "service_date",
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": "service_date",
            "f": "json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"ArcGIS error: {payload['error']}")

    vals = [f["attributes"]["service_date"] for f in payload.get("features", [])]
    return pd.to_datetime(pd.Series(vals), unit="ms").dt.normalize().sort_values()


def fetch_gated_entries(
    start_date: str,
    end_date: str,
    station_names: list[str] | None = None,
    max_rows: int | None = None,
) -> pd.DataFrame:
    """Entries between two dates ('YYYY-MM-DD'), inclusive, paging through the service.

    Date literals must be `timestamp '...'`; ArcGIS accepts `DATE '...'` here but
    it silently returns zero rows. The end bound uses 23:59:59 because
    service_date is stored at LOCAL midnight (04:00/05:00 UTC), so
    `<= timestamp '<end>'` would drop the final day.
    """
    where = (
        f"service_date >= timestamp '{start_date} 00:00:00'"
        f" AND service_date <= timestamp '{end_date} 23:59:59'"
    )
    if station_names:
        quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in station_names)
        where += f" AND station_name IN ({quoted})"

    records: list[dict] = []
    offset = 0
    while True:
        resp = requests.get(
            SERVICE_URL,
            params={
                "where": where,
                "outFields": "*",
                "returnGeometry": "false",
                "orderByFields": "service_date,time_period,station_name",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
                "f": "json",
            },
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()

        if "error" in payload:
            raise RuntimeError(f"ArcGIS error: {payload['error']}")

        page = [f["attributes"] for f in payload.get("features", [])]
        records.extend(page)

        if len(page) < PAGE_SIZE or (max_rows and len(records) >= max_rows):
            break
        offset += PAGE_SIZE

    if max_rows:
        records = records[:max_rows]

    df = pd.DataFrame(records)
    if df.empty:
        return df

    # ArcGIS returns dates as epoch MILLISECONDS against the layer's time
    # reference, which this service declares as UTC. So the raw parse carries a
    # bogus time-of-day (midnight UTC, or 05:00 depending on how it was loaded).
    # Normalize to the calendar date before building any local timestamp,
    # otherwise that offset leaks into period_start.
    df["service_date"] = pd.to_datetime(
        df["service_date"], unit="ms", errors="coerce"
    ).dt.normalize()
    df["gated_entries"] = pd.to_numeric(df["gated_entries"], errors="coerce")

    # time_period is the START of a 30-minute bin, wrapped in parentheses:
    # "(08:30:00)". Strip everything that isn't the digits and colons.
    cleaned = df["time_period"].str.strip().str.strip("()").str.strip()
    parts = cleaned.str.split(":", expand=True)
    hours = pd.to_numeric(parts[0], errors="coerce")
    minutes = pd.to_numeric(parts[1], errors="coerce")

    bad = hours.isna() | minutes.isna()
    if bad.any():
        raise ValueError(
            "Unparseable time_period values: "
            f"{df.loc[bad, 'time_period'].unique()[:5].tolist()}"
        )

    # Service day runs 03:00 -> 02:59, but the field only spans 00:00-23:30,
    # so the early-morning bins belong to the NEXT calendar day.
    rollover = (hours < SERVICE_DAY_START_HOUR).astype(int)

    df["period_start"] = (
        df["service_date"]
        + pd.to_timedelta(rollover, unit="D")
        + pd.to_timedelta(hours, unit="h")
        + pd.to_timedelta(minutes, unit="m")
    ).dt.tz_localize(LOCAL_TZ, nonexistent="shift_forward", ambiguous="NaT")

    return df.drop(columns=["ObjectId"], errors="ignore")


def to_hourly(df: pd.DataFrame, by_line: bool = False) -> pd.DataFrame:
    """Collapse the two 30-minute bins per hour, and (by default) the split-station
    per-line rows, so this joins 1:1 to hourly weather."""
    keys = ["stop_id", "station_name"]
    if by_line:
        keys.append("route_or_line")

    hourly = (
        df.assign(hour_local=df["period_start"].dt.floor("h"))
        .groupby(keys + ["hour_local"], as_index=False)["gated_entries"]
        .sum()
    )
    return hourly.sort_values(["hour_local"] + keys).reset_index(drop=True)



dates = list_service_dates()
print(f"service dates available: {len(dates)}")
print(f"range: {dates.iloc[0].date()} .. {dates.iloc[-1].date()}")

# Pick real dates from the live window rather than hard-coding any.
start = dates.iloc[-2].strftime("%Y-%m-%d")
end = dates.iloc[-1].strftime("%Y-%m-%d")
print(f"\nfetching {start} .. {end}")

df = fetch_gated_entries(start, end, max_rows=4000)
print(f"shape: {df.shape}")
print(f"columns: {list(df.columns)}")
print(df.head(10).to_string())

hourly = to_hourly(df)
print(f"\nhourly shape: {hourly.shape}")
print(hourly.head(10).to_string())
print(f"\nstations: {df['station_name'].nunique()}")

print(df)