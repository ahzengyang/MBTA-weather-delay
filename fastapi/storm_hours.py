"""Weather in its hour by hour windows for the worst events.

storm_days.py ranks whole days. In this, for each top event it
grabs every hour with its conditions matched and writes the
exact time windows we need to pull delay data for.
"""
import sqlite3
import pandas as pd
from user_definition import *

def load_hourly_weather(days: list) -> pd.DataFrame:
    placeholders = ",".join("?" * len(days))
    connection = sqlite3.connect(database_path)
    data = pd.read_sql(f"""
        SELECT substr(observed_at_local,1,10) AS day,
               CAST(substr(observed_at_local,12,2) AS INT) AS hour_local,
               MIN(observed_at_utc) AS utc_hour,
               ROUND(AVG(temperature_2m),1) AS temp_c,
               ROUND(AVG(rain),2) AS rain_mm,
               ROUND(AVG(snowfall),2) AS snow_cm,
               ROUND(MAX(wind_gusts_10m),0) AS gust_kmh,
               ROUND(MIN(visibility)/1000.0,1) AS vis_km,
               SUM(is_raining) AS stations_raining,
               SUM(is_snowing) AS stations_snowing
        FROM weather_hourly
        WHERE substr(observed_at_local,1,10) IN ({placeholders})
        GROUP BY day, hour_local ORDER BY day, hour_local
    """, connection, params=days)
    connection.close()
    return data

def flag_peaks_and_severity(data: pd.DataFrame) -> pd.DataFrame:
    data["peak"] = ""
    data.loc[data.hour_local.isin(am_peak), "peak"] = "AM"
    data.loc[data.hour_local.isin(pm_peak), "peak"] = "PM"
    data["severe"] = ((data.snow_cm > 0.3)
                      | (data.gust_kmh > 60)
                      | (data.vis_km < 1.0)
                      | (data.rain_mm > 2.0))
    return data

if __name__ == '__main__':
    days = pd.read_csv(storm_days_file).head(top_n_days)["day"].tolist()
    hourly = flag_peaks_and_severity(load_hourly_weather(days))
    hourly.to_csv(storm_hours_file, index=False)

    windows = hourly[hourly.severe & (hourly.peak != "")].copy()
    windows[["day", "hour_local", "utc_hour", "peak", "snow_cm", "rain_mm",
             "gust_kmh", "vis_km"]].to_csv(priority_windows_file, index=False)

    print(f"HOURLY BREAKDOWN -- top {top_n_days} events, "
          f"{len(hourly)} hours\n")
    for day in days[:3]:
        print(f"=== {day} ===")
        print(hourly[hourly.day == day][["hour_local", "peak", "temp_c",
                                         "rain_mm", "snow_cm", "gust_kmh",
                                         "vis_km", "severe"]]
              .to_string(index=False))
        print()

    print("=" * 72)
    print(f"severe hours across the {top_n_days} events: "
          f"{hourly.severe.sum()} of {len(hourly)}")
    print(f"severe hours landing in AM/PM peak:        {len(windows)}")
    print(f"\nwrote {storm_hours_file} ({len(hourly)} rows)")
    print(f"wrote {priority_windows_file} ({len(windows)} rows)")
    print(f"\nSOURCE 1: pull delay data for the utc_hour values in "
          f"{priority_windows_file}")
