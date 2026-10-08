"""
Pipeline to fetch data, clean, compare to baseline, and import into CSV
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

import gcs
from gse_baseline import baseline, compare
from gse_clean import clean, daily_totals, hourly_totals
from gse_fetch import available_range, date_range, load, to_dates
from stations import STOP_IDS

EVENTS_FILE = "storm_days.csv"
BASELINE_DAYS = 365

VS_FILE = "GSE_event_vs_baseline.csv"
HOURLY_ALL_FILE = "GSE_hourly_all.csv"
HOURLY_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
VS_DATE_FORMAT = "%Y-%m-%d"


@dataclass
class Result:
    vs_baseline: pd.DataFrame
    hourly_all: pd.DataFrame
    event_dates: list[pd.Timestamp]
    dropped_dates: list[pd.Timestamp]
    baseline_start: pd.Timestamp
    baseline_end: pd.Timestamp


def read_dates_file(path: str) -> list[pd.Timestamp]:
    df = pd.read_csv(path)
    col = next((c for c in ("date", "day") if c in df.columns), df.columns[0])
    return to_dates(pd.to_datetime(df[col]))


def run(events: Iterable, local: str | None = None,
        baseline_start=None, baseline_end=None,
        workers: int = 8, stations: Iterable[str] | None = STOP_IDS) -> Result:
    """Fetch, clean and compare. stations=None keeps every GSE station."""
    events = to_dates(events)

    # calendar date D needs service dates D-1 and D (see gse_fetch)
    lo, hi = available_range(local)
    print(f"GSE data covers service dates {lo.date()} .. {hi.date()}")
    dropped = [d for d in events if not (lo + pd.Timedelta(days=1) <= d <= hi)]
    events = [d for d in events if d not in dropped]
    if dropped:
        print(f"WARNING: dropping {len(dropped)} event date(s) with no GSE data: "
              f"{[str(d.date()) for d in dropped]}")
    if not events:
        raise ValueError("none of the event dates have GSE data")

    end = pd.Timestamp(baseline_end) if baseline_end else hi
    start = pd.Timestamp(baseline_start) if baseline_start else end - pd.Timedelta(days=BASELINE_DAYS - 1)
    base_days = [d for d in date_range(start, end) if d not in set(events)]
    print(f"Event dates: {[str(d.date()) for d in events]}")
    print(f"Baseline {start.date()}..{end.date()} ({len(base_days)} days, event dates excluded)")

    # one pull covering both sets of days
    wanted = sorted(set(events) | set(base_days))
    raw = load(wanted, local, workers)
    print(f"Raw rows: {len(raw):,}")
    if stations is not None:
        keep = raw["stop_id"].isin(set(stations))
        print(f"Keeping {raw.loc[keep, 'stop_id'].nunique()} stations, "
              f"dropping {raw.loc[~keep, 'stop_id'].nunique()}")
        raw = raw[keep]
    all_clean = clean(raw, wanted)
    day = all_clean["datetime"].dt.normalize()

    event_clean = all_clean[day.isin(events)]
    base = baseline(daily_totals(all_clean[day.isin(base_days)]))

    hourly_all = hourly_totals(all_clean)
    hourly_all["event_day"] = hourly_all["datetime"].dt.normalize().isin(events)
    hourly_all = hourly_all[["datetime", "day_type", "event_day", "stop_id",
                             "station_name", "lines", "hourly_entries"]]
    return Result(
        vs_baseline=compare(daily_totals(event_clean), base),
        hourly_all=hourly_all,
        event_dates=events,
        dropped_dates=dropped,
        baseline_start=start,
        baseline_end=end,
    )


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dates", nargs="+", help="event dates, YYYY-MM-DD")
    g.add_argument("--dates-file", default=EVENTS_FILE,
                   help=f"CSV with a 'date' or 'day' column (default {EVENTS_FILE})")
    p.add_argument("--local", help="path to GSE.csv instead of the API")
    p.add_argument("--baseline-start", help=f"default: {BASELINE_DAYS} days before --baseline-end")
    p.add_argument("--baseline-end", help="default: last available service date")
    p.add_argument("--all-stations", action="store_true",
                   help="keep every GSE station, not just the 69 in stations.py")
    p.add_argument("--out-dir", default=".")
    p.add_argument("--upload", action="store_true", help="also write the CSVs to the GCS bucket")
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args()

    events = a.dates if a.dates else read_dates_file(a.dates_file)
    res = run(events, a.local, a.baseline_start, a.baseline_end, a.workers,
              stations=None if a.all_stations else STOP_IDS)

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res.vs_baseline.to_csv(out / VS_FILE, index=False, date_format=VS_DATE_FORMAT)
    res.hourly_all.to_csv(out / HOURLY_ALL_FILE, index=False, date_format=HOURLY_DATE_FORMAT)
    print(f"Saved {len(res.vs_baseline):,} rows -> {out / VS_FILE}")
    print(f"Saved {len(res.hourly_all):,} rows -> {out / HOURLY_ALL_FILE}")

    if a.upload:
        print("Uploaded ->", gcs.save_csv(res.vs_baseline, VS_FILE, VS_DATE_FORMAT))
        print("Uploaded ->", gcs.save_csv(res.hourly_all, HOURLY_ALL_FILE, HOURLY_DATE_FORMAT))


if __name__ == "__main__":
    main()
