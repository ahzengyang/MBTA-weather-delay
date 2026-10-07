"""Goal was to get weather data for every MBTA subway station since June 1st, 2025 on an hourly basis.

Covers the all 125 subway stations as the MBTA defines it, which is the red line, orange line, blue line, the four
green line branches and Mattapan.

Uses the open-meteo historical-forecast endpoint which were ~2 km grids and visibility, which covers
2022 to today, so the whole window we are searching for since June 1st, 2025 fits.

Stations that already in the database get skipped, allowing for any
interrupted run to simply be restarted. (This saved me multiple times)

    python backfillweather.py                       - 2025-06-01 -> yesterday
"""
import os
import sqlite3

import pandas as pd

from user_definition import *
from weather_openmeteo import (WeatherFetchError, fetch_weather,
                               save_processed)


def already_done(expected_rows: int) -> set:
    """Stations with a full set of rows already stored."""
    if not os.path.exists(database_path):
        return set()
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT location, COUNT(*) "
                                  "FROM weather_hourly "
                                  "GROUP BY location").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        connection.close()
    return {location for location, count in rows if count >= expected_rows}
    

def export_parquet() -> None:
    connection = sqlite3.connect(database_path)
    data = pd.read_sql("SELECT * FROM weather_hourly", connection)
    connection.close()
    data.to_parquet(parquet_path, index=False)
    print(f"exported {len(data)} rows -> {parquet_path}")


if __name__ == '__main__':
    stations = pd.read_csv(stations_file)
    expected_rows = ((pd.Timestamp(end_date)
                      - pd.Timestamp(start_date)).days + 1) * 24
    done = already_done(expected_rows)

    print(f"fill {start_date}..{end_date} | {len(stations)} stations "
          f"| {expected_rows} rows each")
    if done:
        print(f"skipping {len(done)} station(s) already complete")

    stored = 0
    failed = []
    for number, station in enumerate(stations.itertuples(index=False), 1):
        if station.station_name in done:
            print(f"[{number}/{len(stations)}] {station.station_name} skip")
            stored += 1
            continue
        try:
            weather = fetch_weather(station.latitude,
                                    station.longitude,
                                    start_date,
                                    end_date,
                                    include_visibility=True)
            weather.insert(0, "location", station.station_name)
            save_processed(weather)
            print(f"[{number}/{len(stations)}] {station.station_name} "
                  f"{len(weather)} rows")
            stored += 1
        except (WeatherFetchError, ValueError) as e:
            print(f"[{number}/{len(stations)}] {station.station_name} "
                  f"FAILED {e}")
            failed.append(station.station_name)

    print(f"=== {stored}/{len(stations)} stations stored, "
          f"{len(failed)} failed ===")
    if failed:
        print(f"failed: {failed}")
        print("re-run this script; completed stations are skipped")

    export_parquet()


if __name__ == "__main__":
    raise SystemExit(main())
