"""
Station map for the MBTA weather/delay dashboard.
with some TODO improvement ideas
"""

import plotly.graph_objects as go
import pandas as pd
from datetime import timedelta

LAT_COL = "station_stop_lat"
LON_COL = "station_stop_lon"
DELAY_COL = "excess_travel_min_mean"
STATION_COL = "station_stop_name"

def build_station_map(df: pd.DataFrame, week_start, benchmark_delay: float) -> go.Figure:

    week_end = week_start + timedelta(days=7)
    week_mask = (df["hour_utc_dt"] >= pd.Timestamp(week_start, tz="UTC")) & (
        df["hour_utc_dt"] < pd.Timestamp(week_end, tz="UTC")
    )
    week_df = df.loc[week_mask]

    station_week_delay = week_df.groupby(STATION_COL)[DELAY_COL].mean()
    station_coords = df.drop_duplicates(STATION_COL).set_index(STATION_COL)[[LAT_COL, LON_COL]]

    plot_df = station_coords.join(station_week_delay.rename("weekly_delay"))
    benchmark_by_station = df.groupby(STATION_COL)[DELAY_COL].mean()
    plot_df["status"] = plot_df.apply(
        lambda r: "delayed" if pd.notna(r["weekly_delay"]) and r["weekly_delay"] > benchmark_by_station.get(r.name, float("inf")) else "on_time",
        axis=1,
    )
    # TODO: No delay data, NA, is also just grey like on time data. 
    # Also on time is any station whos weekl avg delay is more extreme
    # than that station's yearly avg delay 

    fig = go.Figure()

    # TODO: could add subway line grey lines under the dots.
    # However needs GTFS shapes.txt which I would need to pull and join on
    # stations_data 

    for status, color in [("on_time", "grey"), ("delayed", "red")]:
        subset = plot_df[plot_df["status"] == status]
        fig.add_trace(
            go.Scattermap(
                lat=subset[LAT_COL],
                lon=subset[LON_COL],
                mode="markers",
                marker=dict(size=10, color=color),
                text=subset.index,  # station name on hover
                name=status,
                # TODO: could add click action returning to station specific dashboard
            )
        )

    fig.update_layout(
        map=dict(
            style="open-street-map",
            center=dict(lat=42.3601, lon=-71.0589),
            zoom=10.5,
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=True,
    )
    return fig

