"""
Streamlit MBTA weather delay dashboard.

Description: 

Run: streamlit run app.py
"""

from datetime import date, timedelta

import user_definition as cfg
import station_map as smap
import overview as ov
import station_file as sf
import weather_eda as wx
import io

import numpy as np
import pandas as pd
import streamlit as st

from google.cloud import storage
from google.oauth2 import service_account

st.set_page_config(page_title="MBTA weather & delay dashboard", layout="wide")

# How long to keep the GCS data (and everything computed from it) before re-reading.
# Without an expiry, a running app never picks up new rows added to combined_data.csv.
DATA_TTL = "1h"

# ---------------------------------------------------------------------------
# Dataset Import
# ---------------------------------------------------------------------------


@st.cache_resource
def get_gcs_client(service_account_file_path: str, project_id: str) -> storage.Client:
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file_path)
    return storage.Client(project=project_id, credentials=credentials)


@st.cache_data(ttl=DATA_TTL)
def load_mbta_data(
    service_account_file_path: str, project_id: str, bucket_name: str, blob_path: str
) -> pd.DataFrame:
    client = get_gcs_client(service_account_file_path, project_id)
    bucket = client.bucket(bucket_name)

    parquet_path = blob_path.rsplit(".", 1)[0] + ".parquet"
    blob = bucket.blob(parquet_path)
    if blob.exists():
        file_bytes = blob.download_as_bytes()
        print(f"Successfully acquired data from gcs://{bucket_name}/{parquet_path}")
        return pd.read_parquet(io.BytesIO(file_bytes))

    file_bytes = bucket.blob(blob_path).download_as_bytes()
    print(f"Successfully acquired data from gcs://{bucket_name}/{blob_path}")
    return pd.read_csv(io.BytesIO(file_bytes), low_memory=False)


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
# week number matching WEEK_OPTIONS, used to build the "typical week" baseline
MTBS_data["week_idx"] = (pd.to_datetime(
    MTBS_data["date_local"]) - data_start).dt.days // 7

print(MTBS_data.info())

# Helper functions


def week_label(week_start: date) -> str:
    week_last_day = week_start + timedelta(days=6)
    return f"{week_start.strftime('%b %-d')} - {week_last_day.strftime('%b %-d %Y')}"


def filter_week_station(df, week_start, station):
    # whole local (Boston) days, so a week is exactly 7 calendar days
    week_end = week_start + timedelta(days=7)
    mask = (df["date_local"] >= week_start.isoformat()) & (
        df["date_local"] < week_end.isoformat())
    week_df = df[mask]
    if station != "All":
        week_df = week_df[week_df["station_stop_name"] == station]
    return week_df


# Weather vs delay logic
MTBS_data["weather_condition"] = wx.derive_weather_condition(MTBS_data)


def filter_weather_scope(week_start: date, station: str, full_range: bool) -> pd.DataFrame:
    if full_range:
        return MTBS_data if station == "All" else MTBS_data[MTBS_data["station_stop_name"] == station]
    return filter_week_station(MTBS_data, week_start, station)


@st.cache_data(ttl=DATA_TTL)
def load_weather_correlations(week_start: date, station: str, full_range: bool) -> pd.DataFrame:
    return wx.compute_weather_correlations(filter_weather_scope(week_start, station, full_range))


@st.cache_data(ttl=DATA_TTL)
def load_delay_by_bin(week_start: date, station: str, full_range: bool, var: str) -> pd.DataFrame:
    return wx.compute_delay_by_bin(filter_weather_scope(week_start, station, full_range), var)


@st.cache_data(ttl=DATA_TTL)
def load_condition_box_stats(week_start: date, station: str, full_range: bool, by_underground: bool) -> pd.DataFrame:
    groups = ["weather_condition", "station_is_underground"] if by_underground else [
        "weather_condition"]
    return wx.compute_box_stats(filter_weather_scope(week_start, station, full_range), groups)


# ---------------------------------------------------------------------------
# Streamlit Dashboard Start
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Controls -- sidebar shows the main dashboard controls, with a button to
# switch to the weather vs delay page
# ---------------------------------------------------------------------------

st.session_state.setdefault("view", "main")
# Re-assigning widget keys keeps their values while the widget is hidden on the other page
for _key in ("week_select", "station_select", "wx_one_week"):
    if _key in st.session_state:
        st.session_state[_key] = st.session_state[_key]


def go_to(view: str):
    st.session_state["view"] = view


def show_all_stations():
    st.session_state["station_select"] = "All"


view = st.session_state["view"]

with st.sidebar:
    if view == "main":
        st.header("Main dashboard")
        selected_week = st.selectbox(
            "Week", WEEK_OPTIONS, format_func=week_label, key="week_select")
        selected_station = st.selectbox(
            "Station", STATIONS, key="station_select")
        st.divider()
        st.markdown("**How does weather affect delays?**")
        st.button("Weather vs delay  →", on_click=go_to, args=(
            "weather",), type="primary", width="stretch")
    else:
        st.button("←  Main dashboard", on_click=go_to,
                  args=("main",), width="stretch")
        st.header("Weather vs delay")
        selected_station = st.selectbox(
            "Station", STATIONS, key="station_select")
        one_week = st.toggle("Look at a single week", key="wx_one_week")
        selected_week = (
            st.selectbox("Week", WEEK_OPTIONS,
                         format_func=week_label, key="week_select")
            if one_week else st.session_state.get("week_select", WEEK_OPTIONS[0])
        )


# ---------------------------------------------------------------------------
# Weather vs delay page -- EDA across weather variables
# ---------------------------------------------------------------------------

if view == "weather":
    full_range = not one_week
    scope = "all stations" if selected_station == "All" else selected_station
    period = "all dates" if full_range else week_label(selected_week)
    st.title("How does weather affect delays?")
    st.caption(
        f"Showing {scope} · {period}. \"Extra minutes\" is how much longer a trip took than usual."
    )

    # 1. Correlation triage
    st.subheader("1. Which weather matters most?")
    st.caption(
        "Longer bars mean that kind of weather goes along with delays more closely.")
    corr_df = load_weather_correlations(
        selected_week, selected_station, full_range)
    st.plotly_chart(wx.build_correlation_chart(corr_df), width="stretch")

    # 2. Delay by magnitude -- small multiples, strongest link first
    st.subheader("2. Does more of it mean more delay?")
    st.caption(
        "Each chart groups hours by how strong the weather was. The solid line is a typical trip, "
        "the dotted line is a bad trip (the worst 10%). Hollow points have little data behind them, "
        "so treat them with caution. The shaded bars show how many hours fall in each group."
    )
    ordered_vars = corr_df["variable"].tolist()
    for i in range(0, len(ordered_vars), 2):
        cols = st.columns(2)
        for col, var in zip(cols, ordered_vars[i:i + 2]):
            bin_df = load_delay_by_bin(
                selected_week, selected_station, full_range, var)
            with col:
                if bin_df.empty:
                    st.info(f"No data for {wx.WEATHER_VARS[var][0]}.")
                else:
                    st.plotly_chart(wx.build_magnitude_chart(
                        bin_df, var), width="stretch")

    # 3. Distribution by weather condition
    st.subheader("3. How do trips compare across weather conditions?")
    st.caption(
        "Each box holds the middle half of trips; the line inside is the typical trip. "
        "The whiskers show the usual range, leaving out rare extremes."
    )
    box_col1, box_col2 = st.columns(2)
    with box_col1:
        stats = load_condition_box_stats(
            selected_week, selected_station, full_range, False)
        st.plotly_chart(wx.build_condition_box(stats), width="stretch")
    with box_col2:
        stats_ug = load_condition_box_stats(
            selected_week, selected_station, full_range, True)
        st.plotly_chart(wx.build_condition_box_by_underground(
            stats_ug), width="stretch")
    st.stop()


# ---------------------------------------------------------------------------
# Main dashboard logic
# ---------------------------------------------------------------------------

@st.cache_data(ttl=DATA_TTL)
def load_overview(week_start: date) -> dict:
    week_df = filter_week_station(MTBS_data, week_start, "All")
    all_daily = ov.daily_summary(MTBS_data)
    return {
        "week": ov.week_metrics(week_df),
        "typical": ov.typical_week_metrics(MTBS_data),
        "coverage": ov.ridership_coverage(week_df),
        "weather": ov.weather_summary(week_df),
        "days": ov.daily_summary(week_df),
        "typical_day": float(all_daily["delay"].median()),
        "vs_usual": ov.station_vs_usual(week_df, MTBS_data),
        "hourly": ov.hourly_profile(week_df),
    }


@st.cache_data(ttl=DATA_TTL)
def load_network_by_condition() -> pd.DataFrame:
    return sf.grouped_wmean(MTBS_data, "weather_condition")


@st.cache_data(ttl=DATA_TTL)
def load_station(week_start: date, station: str) -> dict:
    station_df = MTBS_data[MTBS_data["station_stop_name"] == station]
    week_df = filter_week_station(MTBS_data, week_start, station)
    overview = load_overview(week_start)
    return {
        "info": sf.station_info(station_df),
        "week": ov.week_metrics(week_df),
        "typical": ov.typical_week_metrics(station_df),
        "busiest_day": sf.busiest_day(week_df),
        "rank": sf.station_rank(overview["vs_usual"], station),
        "weeks": sf.weekly_series(station_df, data_start),
        "week_idx": (pd.Timestamp(week_start) - data_start).days // 7,
        "network_typical": overview["typical"]["extra_trip"],
        "days": ov.daily_summary(week_df) if len(week_df) else pd.DataFrame(),
        "typical_day": float(ov.daily_summary(station_df)["delay"].median()),
        "conditions": sf.condition_comparison(station_df, load_network_by_condition()),
        "hourly": sf.hourly_delay(week_df, station_df),
        "riders_by_hour": sf.hourly_riders(week_df),
    }


def open_clicked_station():
    """Map click -> open that station's page (runs before the rerun, so widgets can be updated)."""
    name = smap.clicked_station(st.session_state["station_map"].selection)
    if name in STATIONS:
        st.session_state["station_select"] = name


def headline_tile(col, label, value, change, unit, note, delta_color="inverse"):
    """`change` vs a typical week; no arrow when it rounds to zero."""
    fmt = "{:+.0f}" if unit == "%" else "{:+.1f}"
    if pd.isna(change):
        delta, arrow = None, "off"
    elif fmt.format(change).lstrip("+-").strip("0.") == "":
        delta, arrow, delta_color = "same as typical week", "off", "off"
    else:
        delta, arrow = f"{fmt.format(change)}{unit if unit == '%' else ' ' + unit} vs typical week", "auto"
    with col.container(border=True):
        st.metric(label, value, delta,
                  delta_color=delta_color, delta_arrow=arrow)
        st.caption(note)


def fmt_min(v):
    return "No data" if pd.isna(v) else f"{v:.1f} min"


def render_headline_tiles(wk: dict, typ: dict, riders_note: str, riders_decimals: int = 0):
    t1, t2, t3, t4 = st.columns(4)
    if pd.isna(wk["riders"]):
        with t1.container(border=True):
            st.metric("Riders this week", "No data")
            st.caption("No ridership data for this week")
    else:
        riders_pct = (wk["riders"] / typ["riders"] - 1) * \
            100 if typ["riders"] else float("nan")
        headline_tile(
            t1, "Riders this week", f"{wk['riders'] / 1000:,.{riders_decimals}f}k", riders_pct, "%",
            riders_note, delta_color="off",
        )
    headline_tile(
        t2, "Extra trip time", fmt_min(
            wk["extra_trip"]), wk["extra_trip"] - typ["extra_trip"], "min",
        "How much longer a trip took than usual",
    )
    headline_tile(
        t3, "Extra wait for a train", fmt_min(
            wk["extra_wait"]), wk["extra_wait"] - typ["extra_wait"], "min",
        "Trains came this much later than scheduled",
    )
    headline_tile(
        t4, "Bad trip (worst 10%)", fmt_min(
            wk["bad_trip"]), wk["bad_trip"] - typ["bad_trip"], "min",
        "1 in 10 trips ran at least this late",
    )


# ---------------------------------------------------------------------------
# Overview page -- shown when "All" stations is selected
# ---------------------------------------------------------------------------

if selected_station == "All":
    st.title("MBTA weather & delay dashboard")
    data = load_overview(selected_week)
    wk, typ = data["week"], data["typical"]
    st.caption(
        f"Week of {week_label(selected_week)} · all stations · compared with a typical week in the data")

    # Row 1: headline tiles
    with_data, n_stations = data["coverage"]
    render_headline_tiles(
        wk, typ, f"Ridership data at {with_data} of {n_stations} stations")

    # Row 2: clickable map + weather and worst stations
    map_col, side_col = st.columns([1.35, 1], gap="large")
    with map_col:
        st.subheader("Where were delays this week?")
        st.caption(
            "Each station vs its own usual week; darker = more delay than usual. **Click a station to see its details.**")
        st.markdown(smap.legend_html(), unsafe_allow_html=True)
        st.pydeck_chart(
            smap.build_station_map(MTBS_data, data["vs_usual"]), height=470,
            key="station_map", on_select=open_clicked_station, selection_mode="single-object",
        )
    with side_col:
        st.subheader("This week's weather")
        st.markdown(ov.weather_strip_html(
            data["weather"]), unsafe_allow_html=True)
        st.subheader("Worst stations this week")
        st.caption("Extra trip time vs that station's usual.")
        st.markdown(ov.worst_stations_html(
            data["vs_usual"]), unsafe_allow_html=True)

    # Row 3: day by day
    st.subheader("Day by day")
    st.caption(
        "Bars show each day's extra trip time, shaded by that day's weather; the dashed line is a typical day. "
        "The panel below shows that day's temperature range."
    )
    st.plotly_chart(ov.build_day_by_day(
        data["days"], data["typical_day"]), width="stretch")

    # Row 4: rush hour
    st.subheader("Rush hour vs off-peak")
    st.caption(
        "Extra trip time by hour of day this week. Dark bars are rush hours (7–9am, 4–6pm).")
    st.plotly_chart(ov.build_rush_hour(data["hourly"]), width="stretch")

# ---------------------------------------------------------------------------
# Station detail page -- shown when a specific station is selected
# ---------------------------------------------------------------------------

else:
    st.button("← All stations", on_click=show_all_stations)
    st.title(selected_station)
    data = load_station(selected_week, selected_station)
    info = data["info"]
    st.markdown(sf.badges_html(info), unsafe_allow_html=True)
    link = f" · [MBTA station page ↗]({info['url']})" if pd.notna(
        info["url"]) else ""
    st.caption(
        f"Week of {week_label(selected_week)} · compared with this station's typical week{link}")

    # Headline tiles + rank
    render_headline_tiles(
        data["week"], data["typical"], data["busiest_day"] or "Ridership entering this station", riders_decimals=1,
    )
    if data["rank"]:
        rank, n_ranked = data["rank"]
        st.markdown(
            f"📍 **#{rank} of {n_ranked} stations** for extra delay this week (vs each station's own normal).")

    # Is this week unusual?
    st.subheader("Is this week unusual?")
    st.caption(
        "Extra trip time for this station every week in the data. The dark bar is the selected week; "
        "the dashed line is the network-wide typical week."
    )
    st.plotly_chart(
        sf.build_weekly_chart(data["weeks"], data["week_idx"], data["network_typical"]), width="stretch"
    )

    # Day by day + weather sensitivity
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Day by day")
        st.caption(
            "This station's extra trip time per day, shaded by weather. Dashed = its typical day.")
        if data["days"].empty:
            st.info("No data for this station this week.")
        else:
            st.plotly_chart(ov.build_day_by_day(
                data["days"], data["typical_day"]), width="stretch")
    with right:
        st.subheader("How does this station handle weather?")
        st.caption(
            "Typical extra trip time by weather, this station vs all stations (whole dataset). "
            f"Faded bars have fewer than {sf.MIN_CONDITION_HOURS} hours of data."
        )
        st.plotly_chart(sf.build_condition_chart(
            data["conditions"], selected_station), width="stretch")
        st.markdown(sf.weather_sentence(
            data["conditions"], selected_station), unsafe_allow_html=True)

    # Time of day
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("When in the day?")
        st.caption(
            "Extra trip time by hour: this week (bars, rush hours dark) vs this station's usual (dashed).")
        st.plotly_chart(sf.build_hourly_delay_chart(
            data["hourly"]), width="stretch")
    with right:
        st.subheader("Riders by hour")
        st.caption(
            "Average riders entering per hour this week. Shows when delays hit the most people.")
        if data["riders_by_hour"].empty:
            st.info("No ridership data for this station this week.")
        else:
            st.plotly_chart(sf.build_riders_chart(
                data["riders_by_hour"]), width="stretch")
