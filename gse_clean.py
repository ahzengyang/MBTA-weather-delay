"""
Step 2: clean raw GSE rows into one row per station per 30-min period.

Output columns:
    datetime       local Eastern wall-clock start of the 30-min period
    stop_id
    station_name
    lines          e.g. "Blue Line, Orange Line" for transfer stations
    gated_entries  summed across lines

What it does
    1. Drops near-duplicate re-loads (same date/period/stop/line), keeping
       the most recent load (highest ObjectId), then drops ObjectId.
    2. Builds `datetime` from service_date + time_period.
       time_period is local time. MBTA's service day runs ~03:00 -> 02:59,
       so periods 00:00-02:30 belong to the NEXT calendar morning.
       e.g. service 2026-06-30 + (00:30:00) -> 2026-07-01 00:30
    3. Transfer stations (State St, Park St, DTX, Gov Ctr, Haymarket,
       North Sta, South Sta) report entries split by line as decimals that
       add up to a whole number. We sum per station. Single-line stations
       are unaffected (summing one row is a no-op).
"""

from __future__ import annotations

import pandas as pd

KEY = ["service_date", "time_period", "stop_id", "route_or_line"]
SERVICE_DAY_START = pd.Timedelta(hours=3)


def drop_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    before = len(df)
    df = (df.sort_values("ObjectId")
            .drop_duplicates(KEY, keep="last")
            .drop(columns="ObjectId"))
    if before - len(df):
        print(f"Dropped {before - len(df):,} duplicate rows")
    return df


def add_datetime(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    t = pd.to_timedelta(df["time_period"].astype(str).str.strip("()"))
    t = t.where(t >= SERVICE_DAY_START, t + pd.Timedelta(days=1))
    df["datetime"] = df["service_date"] + t
    return df.drop(columns=["service_date", "time_period"])


def combine_lines(df: pd.DataFrame) -> pd.DataFrame:
    keys = ["datetime", "stop_id", "station_name"]
    out = (df.groupby(keys, as_index=False)
             .agg(lines=("route_or_line", lambda s: ", ".join(sorted(s.unique()))),
                  gated_entries=("gated_entries", "sum")))
    # remove float noise (e.g. 12.000000001); transfer sums become whole
    out["gated_entries"] = out["gated_entries"].round(2)
    return out


def clean(raw: pd.DataFrame, calendar_dates=None) -> pd.DataFrame:
    """calendar_dates: if given, keep only rows whose local date is in it."""
    df = raw.copy()
    df["gated_entries"] = pd.to_numeric(df["gated_entries"], errors="coerce")
    # a handful of stray Longwood rows (1 entry each) have no stop_id
    missing = df["stop_id"].isna()
    if missing.any():
        print(f"Dropped {missing.sum():,} rows with no stop_id")
        df = df[~missing]
    df = drop_duplicates(df)
    df = add_datetime(df)
    if calendar_dates is not None:
        keep = {pd.Timestamp(d).normalize() for d in calendar_dates}
        df = df[df["datetime"].dt.normalize().isin(keep)]
    df = combine_lines(df)
    return (df[["datetime", "stop_id", "station_name", "lines", "gated_entries"]]
            .sort_values(["datetime", "station_name"])
            .reset_index(drop=True))


def _longest(s: pd.Series) -> str:
    """Most complete line list seen (a line can be missing in quiet periods)."""
    return max(s.unique(), key=len)


def _day_type(ts: pd.Series) -> pd.Series:
    return ts.dt.dayofweek.map(lambda d: "weekend" if d >= 5 else "weekday")


def hourly_totals(clean_df: pd.DataFrame) -> pd.DataFrame:
    """30-min rows -> one row per station per hour (:00 + :30 summed)."""
    df = clean_df.assign(datetime=clean_df["datetime"].dt.floor("h"))
    out = (df.groupby(["datetime", "stop_id", "station_name"], as_index=False)
             .agg(lines=("lines", _longest), hourly_entries=("gated_entries", "sum")))
    out["hourly_entries"] = out["hourly_entries"].round(2)
    out["day_type"] = _day_type(out["datetime"])
    return out


def daily_totals(clean_df: pd.DataFrame) -> pd.DataFrame:
    """30-min rows -> one row per station per calendar day."""
    df = clean_df.assign(date=clean_df["datetime"].dt.normalize())
    out = (df.groupby(["date", "stop_id", "station_name"], as_index=False)
             .agg(lines=("lines", _longest), daily_entries=("gated_entries", "sum")))
    out["daily_entries"] = out["daily_entries"].round(1)
    out["day_type"] = _day_type(out["date"])
    return out
