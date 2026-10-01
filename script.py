import pandas as pd

df = pd.read_csv("GSE.csv", encoding="utf-8-sig")

# Parse dates/times
df["date"] = pd.to_datetime(df["service_date"].str[:10], format="%Y/%m/%d")
df["time"] = df["time_period"].str.strip(
    "()")             # "(00:30:00)" -> "00:30:00"
df["datetime"] = df["date"] + pd.to_timedelta(df["time"])
df["day_of_week"] = df["date"].dt.day_name()

# Rename, drop junk, sort
df = (df.rename(columns={"station_name": "station",
                         "route_or_line": "line",
                         "gated_entries": "entries"})
      [["date", "time", "datetime", "day_of_week",
          "stop_id", "station", "line", "entries"]]
      .sort_values(["datetime", "station", "line"])
      .reset_index(drop=True))

# Optional: one row per station (merges transfer-station line splits)
by_station = df.groupby(["date", "time", "datetime", "day_of_week",
                         "stop_id", "station"], as_index=False)["entries"].sum()

# Optional: wide view (rows = time slot, columns = station)
wide = df.pivot_table(index="datetime", columns="station",
                      values="entries", aggfunc="sum", fill_value=0)

# df.to_csv("GSE_clean.csv", index=False)
