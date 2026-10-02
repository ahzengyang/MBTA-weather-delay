"""
GSE pipeline: fetch -> clean -> baseline -> CSVs  (GCS upload to be added)

Weather-event dates come from storm_entries_2025on.csv ('date' column)
by default. Override with --dates-file or --dates.

Usage
    python run_pipeline.py                        # events from storm_entries_2025on.csv, API
    python run_pipeline.py --local GSE.csv        # same, but read GSE.csv instead of the API
    python run_pipeline.py --dates-file other.csv
    python run_pipeline.py --dates 2025-07-14 2026-01-25

Outputs (in --out-dir, default current folder)
    GSE_events_hourly.csv       hourly entries per station on event dates,
                                plus each station's yearly daily averages
                                (all days / weekday / weekend)
    GSE_event_vs_baseline.csv   per station per event date: daily total vs
                                the same-day-type yearly average
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gse_baseline import attach_baseline, baseline, compare
from gse_clean import clean, daily_totals, hourly_totals
from gse_fetch import date_range, load, to_dates

EVENTS_FILE = "storm_entries_2025on.csv"
BASELINE_START = "2025-07-01"
BASELINE_END = "2026-06-30"


def read_dates_file(path: str) -> list[pd.Timestamp]:
    df = pd.read_csv(path)
    col = next((c for c in ("date", "day") if c in df.columns), df.columns[0])
    return to_dates(pd.to_datetime(df[col]))


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dates", nargs="+", help="event dates, YYYY-MM-DD")
    g.add_argument("--dates-file", default=EVENTS_FILE,
                   help=f"CSV with a 'date' or 'day' column (default {EVENTS_FILE})")
    p.add_argument("--local", help="path to GSE.csv instead of the API")
    p.add_argument("--baseline-start", default=BASELINE_START)
    p.add_argument("--baseline-end", default=BASELINE_END)
    p.add_argument("--out-dir", default=".")
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args()

    events = to_dates(a.dates) if a.dates else read_dates_file(a.dates_file)
    base_days = [d for d in date_range(a.baseline_start, a.baseline_end) if d not in set(events)]
    print(f"Event dates: {[str(d.date()) for d in events]}")
    print(f"Baseline {a.baseline_start}..{a.baseline_end} ({len(base_days)} days, event dates excluded)")

    # one pull covering both sets of days
    wanted = sorted(set(events) | set(base_days))
    raw = load(wanted, a.local, a.workers)
    print(f"Raw rows: {len(raw):,}")
    all_clean = clean(raw, wanted)
    day = all_clean["datetime"].dt.normalize()

    event_clean = all_clean[day.isin(events)]
    base = baseline(daily_totals(all_clean[day.isin(base_days)]))
    combined = attach_baseline(hourly_totals(event_clean), base)
    vs = compare(daily_totals(event_clean), base)

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out / "GSE_events_hourly.csv", index=False, date_format="%Y-%m-%d %H:%M:%S")
    vs.to_csv(out / "GSE_event_vs_baseline.csv", index=False, date_format="%Y-%m-%d")

    print(f"Saved {len(combined):,} rows -> GSE_events_hourly.csv")
    print(f"Saved {len(vs):,} rows -> GSE_event_vs_baseline.csv")


if __name__ == "__main__":
    main()
