"""Rank the weather events worth testing for delay effects.

Just having weather data by itself can't answer our posed argument of weather causing delays, but it can say where the
signal will be. If rain and wind delay trains, it will show on these selected storm days. This is
the handoff to source 1 where they pull delay data for these windows first.
"""
import sqlite3
import pandas as pd

conn = sqlite3.connect("data/processed/weather.db")
df = pd.read_sql("""
    SELECT substr(observed_at_local,1,10) AS day,
           COUNT(DISTINCT location)                       AS stations,
           ROUND(AVG(snowfall)*24,1)                      AS snow_cm,
           ROUND(SUM(is_raining)*1.0/COUNT(DISTINCT location),1) AS rain_hrs,
           ROUND(MAX(wind_gusts_10m),0)                   AS peak_gust_kmh,
           ROUND(MIN(visibility)/1000.0,1)                AS min_vis_km
    FROM weather_hourly GROUP BY day
""", conn)
conn.close()

# A simple severity score so the three hazards are comparable.
df["severity"] = (df.snow_cm / df.snow_cm.max() * 3
                  + df.rain_hrs / df.rain_hrs.max() * 2
                  + df.peak_gust_kmh / df.peak_gust_kmh.max() * 2).round(2)

df["type"] = "rain"
df.loc[df.snow_cm > 1, "type"] = "snow"
df.loc[(df.peak_gust_kmh > 70) & (df.snow_cm <= 1), "type"] = "wind"
df.loc[(df.snow_cm > 1) & (df.peak_gust_kmh > 70), "type"] = "snow+wind"

top = df.sort_values("severity", ascending=False).head(25)
top.to_csv("storm_days.csv", index=False)

print(f"TOP 25 WEATHER EVENTS, {df.day.min()} .. {df.day.max()}")
print("These are the days where a weather->delay effect should be visible.\n")
print(top[["day","type","snow_cm","rain_hrs","peak_gust_kmh","min_vis_km","severity"]]
      .to_string(index=False))

print(f"\n{'-'*72}")
print(f"all days in range:        {len(df):,}")
print(f"days with any rain:       {(df.rain_hrs > 0).sum():,}")
print(f"days with measurable snow:{(df.snow_cm > 1).sum():,}")
print(f"days with gusts > 70km/h: {(df.peak_gust_kmh > 70).sum():,}")
print(f"\nwrote storm_days.csv -- hand this to source 1")
