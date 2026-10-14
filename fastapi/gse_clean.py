"""
Step 2:
Turn raw GSE rows (30 minute periods, one row per station per line)
into one row per station per hour.

Keeps only the stations in stations.py, drops duplicate rows, and merges
transfer stations (e.g. Park St Red + Green) into one row.
"""

import pandas as pd

from stations import STOP_IDS

KEY = ["service_date", "time_period", "stop_id", "route_or_line"]
SERVICE_DAY_START = pd.Timedelta(hours=3)
COLUMNS = ["datetime", "stop_id", "station_name", "lines", "hourly_entries"]


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


def station_lines(df: pd.DataFrame) -> pd.Series:
    """Every line seen at each station, e.g. 'Orange Line, Red Line'."""
    return df.groupby("stop_id")["route_or_line"].agg(lambda s: ", ".join(sorted(s.unique())))


def hourly(raw: pd.DataFrame, calendar_dates=None) -> pd.DataFrame:
    """calendar_dates: if given, keep only rows whose local date is in it."""
    # also drops a handful of stray Longwood rows that have no stop_id
    df = raw[raw["stop_id"].isin(STOP_IDS)].copy()
    df["gated_entries"] = pd.to_numeric(df["gated_entries"], errors="coerce")
    df = add_datetime(drop_duplicates(df))
    if calendar_dates is not None:
        keep = {pd.Timestamp(d).normalize() for d in calendar_dates}
        df = df[df["datetime"].dt.normalize().isin(keep)]

    # :00 and :30 periods, and every line at a transfer station, summed into one row
    df["datetime"] = df["datetime"].dt.floor("h")
    out = (df.groupby(["datetime", "stop_id", "station_name"], as_index=False)
             .agg(hourly_entries=("gated_entries", "sum")))
    # remove float noise (e.g. 12.000000001)
    out["hourly_entries"] = out["hourly_entries"].round(2)
    out["lines"] = out["stop_id"].map(station_lines(df))
    return out[COLUMNS].sort_values(["datetime", "station_name"]).reset_index(drop=True)
