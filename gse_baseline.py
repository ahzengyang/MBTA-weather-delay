"""
Step 3: yearly baseline and weather event comparison.

Ridership averages are only reported in the days the stations reported data
so station closures do not drag down ridership average.
Weather event dates are excluded from the base ridership average
so the normal weather average is not affected.
"""

import pandas as pd

from gse_clean import _longest


def baseline(daily: pd.DataFrame) -> pd.DataFrame:
    keys = ["stop_id", "station_name"]
    allday = daily.groupby(keys).agg(
        lines=("lines", _longest),
        avg_daily_all=("daily_entries", "mean"),
        days_counted=("date", "nunique"),
    )
    by_type = (daily.pivot_table(index=keys, columns="day_type",
                                 values="daily_entries", aggfunc="mean")
                    .rename(columns=lambda c: f"avg_daily_{c}"))
    out = allday.join(by_type).reset_index()
    cols = ["avg_daily_all", "avg_daily_weekday", "avg_daily_weekend"]
    out[cols] = out[cols].round(1)
    return out[["stop_id", "station_name", "lines", *cols, "days_counted"]] \
        .sort_values("station_name").reset_index(drop=True)


def compare(event_daily: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    df = event_daily.merge(
        base[["stop_id", "avg_daily_all", "avg_daily_weekday", "avg_daily_weekend"]],
        on="stop_id", how="left",
    )
    df["baseline_same_day_type"] = df["avg_daily_weekday"].where(
        df["day_type"] == "weekday", df["avg_daily_weekend"])
    df["pct_vs_baseline"] = (
        (df["daily_entries"] / df["baseline_same_day_type"] - 1) * 100).round(1)
    cols = ["date", "day_type", "stop_id", "station_name", "lines", "daily_entries",
            "avg_daily_all", "baseline_same_day_type", "pct_vs_baseline"]
    return df[cols].sort_values(["date", "station_name"]).reset_index(drop=True)
