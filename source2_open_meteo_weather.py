"""
Source 2: Open-Meteo historical hourly weather for Boston (API, no key needed)

Two endpoints, because they trade off differently:

  ARCHIVE (default)  https://archive-api.open-meteo.com/v1/archive
      ERA5 reanalysis, 1940 -> ~5 days ago. Best quality, but ~5-day lag and
      NO `visibility` variable (ERA5 doesn't model it).

  HISTORICAL FORECAST https://historical-forecast-api.open-meteo.com/v1/forecast
      Archived high-resolution forecast runs, 2022 -> today. Includes
      `visibility`, no lag. Use this if you want visibility, or the last week.

Both take start_date / end_date, so unlike the live forecast endpoint you can
request exactly the dates your delay data covers.
"""

from __future__ import annotations

import pandas as pd
import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

LOCAL_TZ = "America/New_York"

# Available on both endpoints.
CORE_VARS = [
    "temperature_2m",
    "precipitation",
    "rain",
    "snowfall",
    "wind_speed_10m",
    "wind_gusts_10m",
    "weather_code",
    "cloud_cover",
]
# Only on the historical-forecast endpoint.
FORECAST_ONLY_VARS = ["visibility"]

# Enough spread to distinguish tunnel from surface running. Swap in the real
# coordinates that source 4 scrapes from each station's Wikipedia infobox.
SAMPLE_LOCATIONS = {
    "Downtown Boston": (42.3554, -71.0605),   # Park St / tunnel core
    "Boston College": (42.3400, -71.1665),    # Green Line B, surface
    "Braintree": (42.2078, -71.0011),         # Red Line, surface
    "Wonderland": (42.4131, -70.9919),        # Blue Line, surface + coastal
}


def fetch_weather(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
    include_visibility: bool = False,
    timezone: str = LOCAL_TZ,
) -> pd.DataFrame:
    """Hourly weather for one point over a date range (dates are 'YYYY-MM-DD')."""
    hourly = list(CORE_VARS)
    url = ARCHIVE_URL
    if include_visibility:
        url = HISTORICAL_FORECAST_URL
        hourly += FORECAST_ONLY_VARS

    resp = requests.get(
        url,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_date,
            "end_date": end_date,
            "hourly": ",".join(hourly),
            "timezone": timezone,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    if "hourly" not in data:
        raise RuntimeError(f"unexpected response: {data}")

    df = pd.DataFrame(data["hourly"]).rename(columns={"time": "observed_at"})

    # Returned in the requested timezone but without an offset; localize so it
    # joins cleanly to the tz-aware arrival timestamps from source 1.
    df["observed_at"] = pd.to_datetime(df["observed_at"]).dt.tz_localize(
        timezone, nonexistent="shift_forward", ambiguous="NaT"
    )
    df["latitude"] = data["latitude"]
    df["longitude"] = data["longitude"]

    df["is_raining"] = df["precipitation"] > 0
    return df


def fetch_weather_for_locations(
    start_date: str,
    end_date: str,
    locations: dict[str, tuple[float, float]] | None = None,
    **kwargs,
) -> pd.DataFrame:
    """Stack hourly weather for several named points into one long DataFrame."""
    locations = locations or SAMPLE_LOCATIONS
    frames = []
    for name, (lat, lon) in locations.items():
        part = fetch_weather(lat, lon, start_date, end_date, **kwargs)
        part.insert(0, "location", name)
        frames.append(part)
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    df = fetch_weather_for_locations("2026-01-01", "2026-01-07")
    print(f"shape: {df.shape}")
    print(df.head(10).to_string())
    print(f"\nrainy hours: {df['is_raining'].sum()} / {len(df)}")
