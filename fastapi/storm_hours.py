"""Weather in its hour by hour windows for the worst events.

storm_days.py ranks whole days. In this, for each top event it
grabs every hour with its conditions matched and writes the
exact time windows we need to pull delay data for.
"""
import sqlite3
import pandas as pd

TOP_N_DAYS = 10
AM_PEAK = range(7, 10)      
PM_PEAK = range(16, 19)     

conn = sqlite3.connect("data/processed/weather.db")

days = pd.read_csv("storm_days.csv").head(TOP_N_DAYS)["day"].tolist()
placeholders = ",".join("?" * len(days))

hourly = pd.read_sql(f"""
    SELECT substr(observed_at_local,1,10)      AS day,
           CAST(substr(observed_at_local,12,2) AS INT) AS hour_local,
           MIN(observed_at_utc)                AS utc_hour,
           ROUND(AVG(temperature_2m),1)        AS temp_c,
           ROUND(AVG(rain),2)                  AS rain_mm,
           ROUND(AVG(snowfall),2)              AS snow_cm,
           ROUND(MAX(wind_gusts_10m),0)        AS gust_kmh,
           ROUND(MIN(visibility)/1000.0,1)     AS vis_km,
           SUM(is_raining)                     AS stations_raining,
           SUM(is_snowing)                     AS stations_snowing
    FROM weather_hourly
    WHERE substr(observed_at_local,1,10) IN ({placeholders})
    GROUP BY day, hour_local ORDER BY day, hour_local
""", conn, params=days)
conn.close()

hourly["peak"] = ""
hourly.loc[hourly.hour_local.isin(AM_PEAK), "peak"] = "AM"
hourly.loc[hourly.hour_local.isin(PM_PEAK), "peak"] = "PM"

hourly["severe"] = ((hourly.snow_cm > 0.3) | (hourly.gust_kmh > 60)
                    | (hourly.vis_km < 1.0) | (hourly.rain_mm > 2.0))

hourly.to_csv("storm_hours.csv", index=False)

windows = hourly[hourly.severe & (hourly.peak != "")].copy()
windows[["day","hour_local","utc_hour","peak","snow_cm","rain_mm","gust_kmh","vis_km"]] \
    .to_csv("priority_windows.csv", index=False)

print(f"HOURLY BREAKDOWN -- top {TOP_N_DAYS} events, {len(hourly)} hours\n")
for day in days[:3]:
    d = hourly[hourly.day == day]
    print(f"=== {day} ===")
    print(d[["hour_local","peak","temp_c","rain_mm","snow_cm","gust_kmh","vis_km","severe"]]
          .to_string(index=False))
    print()

print("=" * 72)
print(f"severe hours across the {TOP_N_DAYS} events: {hourly.severe.sum()} of {len(hourly)}")
print(f"severe hours landing in AM/PM peak:        {len(windows)}   <-- the sharp test")
print(f"\nwrote storm_hours.csv       ({len(hourly)} rows, every hour of every top event)")
print(f"wrote priority_windows.csv  ({len(windows)} rows, severe + rush hour)")
print("\nSOURCE 1: pull delay data for the utc_hour values in priority_windows.csv")
