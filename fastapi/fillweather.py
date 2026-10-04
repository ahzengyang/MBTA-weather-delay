"""Goal was to get weather data for every MBTA subway station since June 1st, 2025 on an hourly basis.

Covers the all 125 subway stations as the MBTA defines it, which is the red line, orange line, blue line, the four
green line branches and Mattapan.

Uses the open-meteo historical-forecast endpoint which were ~2 km grids and visibility, which covers
2022 to today, so the whole window we are searching for since June 1st, 2025 fits.

Stations that already in the database get skipped, allowing for any
interrupted run to simply be restarted. (This saved me multiple times)

    python backfillweather.py                       - 2025-06-01 -> yesterday
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta

import pandas as pd

from weather_openmeteo import (
    DB_PATH, PROCESSED_DIR, WeatherFetchError, fetch_weather, log, save_processed,
)

# All 125 subway parent stations. `stations_heavy_rail.csv` still holds the 52
# Red/Orange/Blue subset if the scope is ever narrowed again.
STATIONS_CSV = "stations.csv"


def _already_done(expected_rows: int) -> set[str]:
    """Stations with a full set of rows already stored."""
    if not DB_PATH.exists():
        return set()
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT location, COUNT(*) FROM weather_hourly GROUP BY location"
        ).fetchall()
    except sqlite3.OperationalError:      # table not created yet
        return set()
    finally:
        conn.close()
    return {loc for loc, n in rows if n >= expected_rows}


def main() -> int:
    p = argparse.ArgumentParser(description="Back-fill MBTA station weather since 2024.")
    p.add_argument("--start", default="2025-06-01")
    p.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    p.add_argument("--stations-csv", default=STATIONS_CSV)
    p.add_argument("--limit", type=int, help="only process the first N stations (testing)")
    p.add_argument("--force", action="store_true", help="re-fetch even if already stored")
    args = p.parse_args()

    stations = pd.read_csv(args.stations_csv)
    if args.limit:
        stations = stations.head(args.limit)

    expected = (pd.Timestamp(args.end) - pd.Timestamp(args.start)).days + 1
    expected_rows = expected * 24
    done = set() if args.force else _already_done(expected_rows)

    log.info("backfill %s..%s | %d stations | %d rows each",
             args.start, args.end, len(stations), expected_rows)
    if done:
        log.info("skipping %d station(s) already complete", len(done))

    ok, failed = 0, []
    for i, st in enumerate(stations.itertuples(index=False), 1):
        name = st.station_name
        if name in done:
            log.info("[%d/%d] %-26s skip (already complete)", i, len(stations), name)
            ok += 1
            continue
        try:
            df = fetch_weather(
                st.latitude, st.longitude, args.start, args.end,
                include_visibility=True,          # -> historical-forecast endpoint
            )
            df.insert(0, "location", name)
            save_processed(df, write_csv=False)
            log.info("[%d/%d] %-26s %d rows", i, len(stations), name, len(df))
            ok += 1
        except (WeatherFetchError, ValueError):
            log.exception("[%d/%d] %-26s FAILED", i, len(stations), name)
            failed.append(name)

    log.info("=== %d/%d stations stored, %d failed ===", ok, len(stations), len(failed))
    if failed:
        log.error("failed: %s", failed)
        log.error("re-run this script; completed stations are skipped automatically")

    # One Parquet export at the end. A 3M-row CSV is ~360 MB; Parquet is a
    # fraction of that and preserves dtypes.
    conn = sqlite3.connect(DB_PATH)
    try:
        out = pd.read_sql("SELECT * FROM weather_hourly", conn)
    finally:
        conn.close()
    path = PROCESSED_DIR / "weather_hourly.parquet"
    out.to_parquet(path, index=False)
    log.info("exported %d rows -> %s", len(out), path)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
