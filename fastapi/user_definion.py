import os
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()
bucket_name = os.getenv('GCP_BUCKET_NAME')
mbta_url = "https://api-v3.mbta.com"
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

raw_directory = "data/raw/weather"
processed_directory = "data/processed"
database_path = "data/processed/weather.db"
parquet_path = "data/processed/weather_hourly.parquet"
stations_file = "stations.csv"
heavy_rail_file = "stations_heavy_rail.csv"
storm_days_file = "storm_days.csv"
storm_hours_file = "storm_hours.csv"
priority_windows_file = "priority_windows.csv"

start_date = "2025-06-01"
end_date = (date.today() - timedelta(days=1)).isoformat()

max_retries = 5
timeout_seconds = 60
retryable_status = {408, 425, 429, 500, 502, 503, 504}

top_n_days = 10
am_peak = range(7, 10)
pm_peak = range(16, 19)
