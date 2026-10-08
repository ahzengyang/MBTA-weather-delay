"""
Pipeline to fetch data, clean, compare to baseline, and import into CSV file
"""

import argparse

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


def read_dates_file(path):
    return to_dates(pd.read_csv(path)["day"])


def run(events, local=None):
    """Fetch, clean and compare to the baseline. Returns a dict of results."""
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

    # baseline is the 365 days ending on the last published date, minus the event dates
    end = hi
    start = end - pd.Timedelta(days=BASELINE_DAYS - 1)
    base_days = [d for d in date_range(start, end) if d not in events]
    print(f"Event dates: {[str(d.date()) for d in events]}")
    print(f"Baseline {start.date()}..{end.date()} ({len(base_days)} days, event dates excluded)")

    # one pull covering both sets of days
    wanted = sorted(set(events) | set(base_days))
    raw = load(wanted, local)
    print(f"Raw rows: {len(raw):,}")
    raw = raw[raw["stop_id"].isin(STOP_IDS)]
    all_clean = clean(raw, wanted)
    day = all_clean["datetime"].dt.normalize()

    event_clean = all_clean[day.isin(events)]
    base = baseline(daily_totals(all_clean[day.isin(base_days)]))

    hourly_all = hourly_totals(all_clean)
    hourly_all["event_day"] = hourly_all["datetime"].dt.normalize().isin(events)
    hourly_all = hourly_all[["datetime", "day_type", "event_day", "stop_id",
                             "station_name", "lines", "hourly_entries"]]
    return {
        "vs_baseline": compare(daily_totals(event_clean), base),
        "hourly_all": hourly_all,
        "event_dates": events,
        "dropped_dates": dropped,
        "baseline_start": start,
        "baseline_end": end,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", help="path to a downloaded GSE.csv instead of the API")
    parser.add_argument("--upload", action="store_true", help="also write the CSVs to the GCS bucket")
    args = parser.parse_args()

    res = run(read_dates_file(EVENTS_FILE), args.local)
    res["vs_baseline"].to_csv(VS_FILE, index=False, date_format=VS_DATE_FORMAT)
    res["hourly_all"].to_csv(HOURLY_ALL_FILE, index=False, date_format=HOURLY_DATE_FORMAT)
    print(f"Saved {len(res['vs_baseline']):,} rows to {VS_FILE}")
    print(f"Saved {len(res['hourly_all']):,} rows to {HOURLY_ALL_FILE}")

    if args.upload:
        print("Uploaded to", gcs.save_csv(res["vs_baseline"], VS_FILE, VS_DATE_FORMAT))
        print("Uploaded to", gcs.save_csv(res["hourly_all"], HOURLY_ALL_FILE, HOURLY_DATE_FORMAT))
