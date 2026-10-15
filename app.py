"""
Streamlit MBTA weather delay dashboard.

Description: 

Run: streamlit run app.py
"""

from datetime import date, timedelta

import io
import numpy as np
import pandas as pd
import streamlit as st
import user_definition as cfg
from station_map import build_station_map

from google.cloud import storage
from google.oauth2 import service_account

st.set_page_config(page_title="MBTA weather & delay dashboard", layout="wide")

# ---------------------------------------------------------------------------
# Dataset Import
# ---------------------------------------------------------------------------

@st.cache_resource
def get_gcs_client(service_account_file_path: str, project_id: str) -> storage.Client:
    credentials = service_account.Credentials.from_service_account_file(service_account_file_path)
    return storage.Client(project=project_id, credentials=credentials)

@st.cache_data
def load_mbta_data(
    service_account_file_path: str, project_id: str, bucket_name: str, blob_path: str
) -> pd.DataFrame:
    client = get_gcs_client(service_account_file_path, project_id)
    blob = client.bucket(bucket_name).blob(blob_path)
    file_bytes = blob.download_as_bytes()
    print(f"Successfully acquired data from gcs://{bucket_name}/{blob_path}")
    return pd.read_csv(io.BytesIO(file_bytes))

MTBS_data = load_mbta_data(
    cfg.service_account_file_path, cfg.project_id, cfg.bucket_name, "combined_data/combined_data.csv"
)

# ---------------------------------------------------------------------------
# Dashboard Logic
# ---------------------------------------------------------------------------

STATIONS = ["All"] + sorted(MTBS_data["station_stop_name"].unique().tolist())

MTBS_data["hour_utc_dt"] = pd.to_datetime(MTBS_data["hour_utc"], utc=True)
MTBS_data["date_local"] = MTBS_data["hour_local"].str[:10]

data_start = pd.to_datetime(MTBS_data["date_local"]).min().normalize()
data_end = pd.to_datetime(MTBS_data["date_local"]).max().normalize()
WEEK_OPTIONS = pd.date_range(data_start, data_end, freq="7D").date.tolist()

# Helper functions
def celsius_to_f(c):
    return c * 9 / 5 + 32

def week_label(week_start: date) -> str:
    week_end = week_start + timedelta(days=7)
    return f"{week_start.strftime('%b %-d')} - {week_end.strftime('%b %-d %Y')}"

def filter_week_station(df, week_start, station):
    week_end = week_start + timedelta(days=7)
    mask = (df["hour_utc_dt"] >= pd.Timestamp(week_start, tz="UTC")) & (
        df["hour_utc_dt"] < pd.Timestamp(week_end, tz="UTC")
    )
    week_df = df[mask]
    if station != "All":
        week_df = week_df[week_df["station_stop_name"] == station]
    return week_df

# Yearly - weekly logic
@st.cache_data
def load_weekly_summary(week_start: date, station: str) -> dict:
    week_df = filter_week_station(MTBS_data, week_start, station)
    year_df = MTBS_data if station == "All" else MTBS_data[MTBS_data["station_stop_name"] == station]

    daily = week_df.groupby("date_local")
    daily_highs = daily["temperature_2m"].max()
    daily_lows = daily["temperature_2m"].min()
    snowy_days = int(daily["is_snowing"].max().sum())

    return {
        "total_riders": int(week_df["hourly_entries"].sum()), 
        # TODO issue with ridership, 54/125 stations have no ridership / toll data so expected 45% NA but 
        # currently there is +15%, 58% NA ridership. June - late Aug 2025 have no ridership data for 
        # ALL stations. Could be issue of my logic pls check!
        "weekly_avg_delay": round(float(week_df["excess_travel_min_mean"].mean()), 1),
        "yearly_avg_delay": round(float(year_df["excess_travel_min_mean"].mean()), 1),
        "avg_high_f": round(celsius_to_f(daily_highs.mean()), 1),
        "avg_low_f": round(celsius_to_f(daily_lows.mean()), 1),
        "snowy_days": snowy_days,
    }


# Weekly - daily logic
@st.cache_data
def load_daily_trend(week_start: date, station: str) -> pd.DataFrame:
    week_df = filter_week_station(MTBS_data, week_start, station)
    daily = week_df.groupby("date_local").agg(
        delay_min=("excess_travel_min_mean", "mean"),
        temp_c=("temperature_2m", "mean"),
    )
    daily["temp_f"] = celsius_to_f(daily["temp_c"])
    return daily.reset_index().rename(columns={"date_local": "day"})


# ---------------------------------------------------------------------------
# Streamlit Dashboard Start
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Controls (shared across both views)
# ---------------------------------------------------------------------------

st.title("MBTA weather & delay dashboard")

ctrl_col1, ctrl_col2 = st.columns(2)
with ctrl_col1:
    selected_week = st.selectbox(
        "Week", WEEK_OPTIONS, format_func=week_label, key="week_select"
    )
with ctrl_col2:
    selected_station = st.selectbox("Station", STATIONS, key="station_select")

summary = load_weekly_summary(selected_week, selected_station)

# ---------------------------------------------------------------------------
# Overview page -- shown when "All" stations is selected
# ---------------------------------------------------------------------------

if selected_station == "All":
    left, right = st.columns([1, 1])

    with left:
        st.metric("Total riders", f"{summary['total_riders']:,}")

        st.subheader("Delay metrics")
        d1, d2 = st.columns(2)
        d1.metric("Weekly avg delay", f"{summary['weekly_avg_delay']} min")
        d2.metric("Yearly avg delay", f"{summary['yearly_avg_delay']} min")

        st.subheader("Weather metrics")
        w1, w2 = st.columns(2)
        w1.metric("Avg high", f"{summary['avg_high_f']}°F")
        w2.metric("Avg low", f"{summary['avg_low_f']}°F")
        st.metric("Snowy days", summary["snowy_days"])

    with right:
        st.subheader("Traffic flow map")
        
        benchmark = MTBS_data["excess_travel_min_mean"].mean()  # TODO: pick real benchmark (see station_map.py notes)
        fig = build_station_map(MTBS_data, selected_week, benchmark)
        st.plotly_chart(fig, use_container_width=True)
        
# ---------------------------------------------------------------------------
# Station detail page -- shown when a specific station is selected
# ---------------------------------------------------------------------------

else:
    st.metric("Total riders", f"{summary['total_riders']:,}")

    daily_df = load_daily_trend(selected_week, selected_station)

    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.subheader("Delay metrics")
        st.line_chart(daily_df.set_index("day")["delay_min"])
    with chart_col2:
        st.subheader("Weather metrics")
        st.line_chart(daily_df.set_index("day")["temp_f"])

    st.divider()
    if st.button("← All stations"):
        st.session_state["station_select"] = "All"
        st.rerun()
