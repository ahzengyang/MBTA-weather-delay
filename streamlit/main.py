"""
MBTA delays and weather — dashboard.

PLACEHOLDER. Loads the combined table and shows what is in it, so the
container can be built and deployed while the real dashboard is written.

Reads combined_data/combined_data.csv, which /combined/refresh writes after
joining delays, stations, ridership and weather. It does not call the API:
the API has no endpoint that serves the combined table, and the collection
endpoints are POST routes for Cloud Scheduler, not for a dashboard to hit.
"""

import io
import os

import pandas as pd
import streamlit as st
from google.cloud import storage

BUCKET = os.getenv("GCP_BUCKET_NAME", "mbta-weather-delay")
COMBINED_BLOB = "combined_data/combined_data.csv"

st.set_page_config(page_title="MBTA delays and weather", layout="wide")


@st.cache_data(ttl=3600, show_spinner="Loading combined data…")
def load_combined() -> pd.DataFrame:
    """Download the combined table from GCS.

    Cached for an hour: the upstream file is rewritten once a day by the
    combined scheduler job, and Streamlit reruns this script on every widget
    interaction.
    """
    blob = storage.Client().bucket(BUCKET).blob(COMBINED_BLOB)
    return pd.read_csv(io.BytesIO(blob.download_as_bytes()), low_memory=False)


st.title("MBTA delays and weather")

try:
    df = load_combined()
except Exception as error:
    st.error(f"Could not load gs://{BUCKET}/{COMBINED_BLOB}")
    st.caption(
        "The combined table is written by POST /combined/refresh, which runs "
        "after the four collectors. If this is a fresh bucket, run the "
        "collectors first."
    )
    st.exception(error)
    st.stop()

left, middle, right = st.columns(3)
left.metric("Rows", f"{len(df):,}")
middle.metric("Columns", df.shape[1])
if "parent_station" in df.columns:
    right.metric("Stations", df["parent_station"].nunique())

# Station filter, so the placeholder exercises the "query/filter" requirement
# rather than only displaying a table.
view = df
if "parent_station" in df.columns:
    stations = sorted(df["parent_station"].dropna().unique())
    picked = st.multiselect("Stations", stations, default=[])
    if picked:
        view = df[df["parent_station"].isin(picked)]

st.caption(f"Showing {len(view):,} of {len(df):,} rows")
st.dataframe(view.head(1000), use_container_width=True)

with st.expander("Columns and types"):
    st.dataframe(
        pd.DataFrame({"column": df.columns, "dtype": df.dtypes.astype(str).values}),
        use_container_width=True,
        hide_index=True,
    )
