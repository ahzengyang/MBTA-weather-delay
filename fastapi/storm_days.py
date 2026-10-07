"""Rank the weather events worth testing for delay effects.

Just having weather data by itself can't answer our posed argument of weather causing delays, but it can say where the
signal will be. If rain and wind delay trains, it will show on these selected storm days. 
"""
import sqlite3
import pandas as pd
from user_definition import *

def load_daily_weather() -> pd.DataFrame:
    connection = sqlite3.connect(database_path)
    data = pd.read_sql("""
        SELECT substr(observed_at_local,1,10) AS day,
               COUNT(DISTINCT location) AS stations,
               ROUND(AVG(snowfall)*24,1) AS snow_cm,
               ROUND(SUM(is_raining)*1.0/COUNT(DISTINCT location),1)
                   AS rain_hrs,
               ROUND(MAX(wind_gusts_10m),0) AS peak_gust_kmh,
               ROUND(MIN(visibility)/1000.0,1) AS min_vis_km
        FROM weather_hourly GROUP BY day
    """, connection)
    connection.close()
    return data

def score_severity(data: pd.DataFrame) -> pd.DataFrame:
    data["severity"] = (data.snow_cm / data.snow_cm.max() * 3
                        + data.rain_hrs / data.rain_hrs.max() * 2
                        + data.peak_gust_kmh / data.peak_gust_kmh.max()
                        * 2).round(2)
    data["type"] = "rain"
    data.loc[data.snow_cm > 1, "type"] = "snow"
    data.loc[(data.peak_gust_kmh > 70) & (data.snow_cm <= 1), "type"] = "wind"
    data.loc[(data.snow_cm > 1)
             & (data.peak_gust_kmh > 70), "type"] = "snow+wind"
    return data

if __name__ == '__main__':
    daily = score_severity(load_daily_weather())
    top = daily.sort_values("severity", ascending=False).head(25)
    top.to_csv(storm_days_file, index=False)

    print(f"TOP 25 WEATHER EVENTS, {daily.day.min()} .. {daily.day.max()}")
    print("These are the days where a weather->delay effect should be "
          "visible.\n")
    print(top[["day", "type", "snow_cm", "rain_hrs", "peak_gust_kmh",
               "min_vis_km", "severity"]].to_string(index=False))

    print(f"\n{'-' * 72}")
    print(f"all days in range:        {len(daily):,}")
    print(f"days with any rain:       {(daily.rain_hrs > 0).sum():,}")
    print(f"days with measurable snow:{(daily.snow_cm > 1).sum():,}")
    print(f"days with gusts > 70km/h: {(daily.peak_gust_kmh > 70).sum():,}")
    print(f"\nwrote {storm_days_file} -- hand this to source 1")
