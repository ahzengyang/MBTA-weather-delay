"""
Shared configuration, merged from the three per-branch versions.

Secrets and anything environment-specific come from .env via os.getenv.
Everything else is a constant checked into the repo, because it describes
the data sources rather than the machine.

.env must be in dotenv format (KEY=value) -- python-dotenv cannot read
YAML. The deploy script converts it to YAML for --env-vars-file.

Consumers:
    mbta_weather_api.py     from user_definition import *
    weather_openmeteo.py    from user_definition import *
    station_data.py         from user_definition import (...)
"""

import os

from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# GCP -- from .env. load_dotenv() is a no-op on Cloud Run, where these
# arrive as real environment variables set at deploy time.
# =====================================================================
project_id = os.getenv("GCP_PROJECT_ID")
bucket_name = os.getenv("GCP_BUCKET_NAME")
service_account_file_path = os.getenv("GCP_SERVICE_ACCOUNT_KEY")

# =====================================================================
# Source 1 -- MBTA LAMP delay panel
# =====================================================================
mbta_url = "https://api-v3.mbta.com"

# =====================================================================
# Source 2 -- Open-Meteo hourly weather
#
# archive_url covers 1940 to yesterday on a ~25 km grid, no visibility.
# historical_forecast_url covers 2022 to today on a ~2 km grid and does
# include visibility, which is why fetch_weather switches to it when
# include_visibility=True.
# =====================================================================
archive_url = "https://archive-api.open-meteo.com/v1/archive"
historical_forecast_url = ("https://historical-forecast-api.open-meteo.com"
                           "/v1/forecast")

local_timezone = "America/New_York"
api_timezone = "GMT"

core_variables = ["temperature_2m", "rain", "snowfall", "wind_gusts_10m"]
forecast_only_variables = ["visibility"]
flag_columns = ["is_precipitating", "is_raining", "is_snowing",
                "is_freezing_precip"]
table_columns = (["location", "observed_at_utc", "observed_at_local",
                  "latitude", "longitude"]
                 + core_variables
                 + forecast_only_variables
                 + flag_columns
                 + ["ingested_at_utc"])

# Collection window. Matches the span of the weather parquet already in
# the bucket; the delay panel needs regenerating to cover the same range.
start_date = "2025-06-01"
end_date = "2026-09-30"

# Retry policy for Open-Meteo.
max_retries = 5
timeout_seconds = 60
retryable_status = {408, 425, 429, 500, 502, 503, 504}

# =====================================================================
# Source 3 -- MBTA GTFS station reference data
# =====================================================================
mbta_gtfs_url = "https://cdn.mbta.com/MBTA_GTFS.zip"
station_data_file_name = "station_data_file"

# =====================================================================
# Local paths
#
# WARNING: these are container-local on Cloud Run. The filesystem is
# ephemeral and not shared between instances, so anything written here
# is lost when the instance recycles and is invisible to other
# instances. Only safe for scratch within a single request.
# =====================================================================
raw_directory = "data/raw/weather"
processed_directory = "data/processed"
database_path = "data/processed/weather.db"
parquet_path = "data/processed/weather_hourly.parquet"

stations_file = "stations.csv"
heavy_rail_file = "stations_heavy_rail.csv"
storm_days_file = "storm_days.csv"
storm_hours_file = "storm_hours.csv"
priority_windows_file = "priority_windows.csv"

# =====================================================================
# GCS prefixes
#
# Note: gcs.py (the ridership chain) reads GCS_PREFIX and GCS_RAW_PREFIX
# from the environment instead of importing these, defaulting to
# mbta_gse/processed and mbta_gse/raw.
# =====================================================================
gcs_processed_prefix = "weather/processed"
gcs_raw_prefix = "weather/raw"

# =====================================================================
# Analysis parameters
# =====================================================================
top_n_days = 10
am_peak = range(7, 10)
pm_peak = range(16, 19)
