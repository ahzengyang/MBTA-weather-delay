"""
Combine delays, station metadata, ridership, and weather into one dataframe.

How joins work:
    delays.parent_station = station_data.stop_id
    delays.parent_station, hour_utc = ridership.stop_id, ridership.datetime
    station (stop_lat, stop_lon) ~ nearest weather (latitude, longitude) *logic implemented to find closest weather recording
    delays.hour_utc = weather.observed_at_utc (same hour)

Data is grabbed from the GCS bucket
The combined dataframe is uploaded to the bukcet at combined_data/combined_data.csv.

How to run: python combine_sources.py
"""
import io
import re

import numpy as np
import pandas as pd


# Object names inside the GCS bucket
STATION_BLOB = "station_processed/station_data_file"
WEATHER_BLOB = "weather/processed/weather_hourly.parquet"
WEATHER_DAILY_PREFIX = "weather/processed/daily/"
RIDERSHIP_BLOB = "mbta_gse/processed/GSE_hourly_all.csv"
DELAYS_PREFIX = "mbta_lamp/processed/mbta_station_hour_panel_"
COMBINED_BLOB = "combined_data/combined_data.csv"
COMBINED_PARQUET_BLOB = "combined_data/combined_data.parquet"

# Matches e.g. mbta_station_hour_panel_2025-10-01_to_2025-10-07.csv
# and single day panels like mbta_station_hour_panel_2025-10-01.csv
_PANEL_RE = re.compile(
    r"mbta_station_hour_panel_(\d{4}-\d{2}-\d{2})(?:_to_(\d{4}-\d{2}-\d{2}))?\.csv$"
)

#The ridership is in EST and the others are in UTC, so we need to manually set the time zone for ridership
RIDERSHIP_NAIVE_TZ = "America/New_York"


#Time conversions
def to_utc_hour(series: pd.Series, naive_tz: str = "UTC") -> pd.Series:
    """
    Parse a column of timestamps into tz-aware UTC datetimes floored to the hour.

    Handles the formats seen in the data:
      delays.hour_utc -> "2025-06-08 01:00:00+00:00"
      weather.observed_at_utc -> "2025-06-01T00:00:00+00:00"
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        dt = series
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize(naive_tz, ambiguous="NaT", nonexistent="shift_forward")
        dt = dt.dt.tz_convert("UTC")
    else:
        s = series.astype("string").str.strip()
        has_offset = s.str.contains(re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$"), na=False)
        dt = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns, UTC]")
        if has_offset.any():
            dt.loc[has_offset] = pd.to_datetime(s[has_offset], utc=True, errors="coerce")
        if (~has_offset).any():
            naive = pd.to_datetime(s[~has_offset], errors="coerce")
            dt.loc[~has_offset] = (
                naive.dt.tz_localize(naive_tz, ambiguous="NaT", nonexistent="shift_forward")
                .dt.tz_convert("UTC")
            )
    return dt.dt.floor("h").astype("datetime64[ns, UTC]")


def clean_id(series: pd.Series) -> pd.Series:
    s = series.astype("string").str.strip()
    return s.str.replace(r"^(\d+)\.0$", r"\1", regex=True)


#Calculate nearest lat and long
def nearest_points(
    query_lat: np.ndarray,
    query_lon: np.ndarray,
    ref_lat: np.ndarray,
    ref_lon: np.ndarray,
):
    #For each query point, return (index of nearest reference point, distance km) using the haversine difference.
    #We can just brute force over all pairs since theres a couple hundred station and weather locations

    qlat = np.radians(query_lat)[:, None]
    qlon = np.radians(query_lon)[:, None]
    rlat = np.radians(ref_lat)[None, :]
    rlon = np.radians(ref_lon)[None, :]

    a = (
        np.sin((rlat - qlat) / 2) ** 2
        + np.cos(qlat) * np.cos(rlat) * np.sin((rlon - qlon) / 2) ** 2
    )
    # 6371.0088 km = mean radius of the Earth
    dist = 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


    idx = dist.argmin(axis=1)
    return idx, dist[np.arange(len(idx)), idx]


#Prep each data source
def prep_delays(delays: pd.DataFrame) -> pd.DataFrame:
    d = delays.copy()
    d["parent_station"] = clean_id(d["parent_station"])
    d["_hour_utc"] = to_utc_hour(d["hour_utc"])
    return d


def prep_stations(station_data: pd.DataFrame) -> pd.DataFrame:
    s = station_data.copy()
    s["stop_id"] = clean_id(s["stop_id"])
    s = s.drop_duplicates(subset="stop_id", keep="first")
    s = s.add_prefix("station_").rename(columns={"station_stop_id": "_station_key"})
    return s


def prep_ridership(ridership: pd.DataFrame, naive_tz: str) -> pd.DataFrame:
    r = ridership.copy()
    r["_station_key"] = clean_id(r["stop_id"])
    r["_hour_utc"] = to_utc_hour(r["datetime"], naive_tz=naive_tz)

    agg = {"hourly_entries": "sum"}
    for col, how in [("day_type", "first"), ("event_day", "max"),
                     ("station_name", "first"), ("lines", "first")]:
        if col in r.columns:
            agg[col] = how
    r = r.groupby(["_station_key", "_hour_utc"], as_index=False, dropna=False).agg(agg)

    return r.rename(columns={
        "station_name": "ridership_station_name",
        "lines": "ridership_lines",
        "day_type": "ridership_day_type",
        "event_day": "ridership_event_day",
    })


def prep_weather(weather_hourly: pd.DataFrame):


    #points: one row per distinct weather location (lat/lon), with an id
    #hourly: weather readings (_weather_point_id, _hour_utc)

    w = weather_hourly.copy()
    w["_hour_utc"] = to_utc_hour(w["observed_at_utc"])

    w["_lat_key"] = w["latitude"].round(5)
    w["_lon_key"] = w["longitude"].round(5)

    points = (
        w[["_lat_key", "_lon_key", "location"]]
        .drop_duplicates(subset=["_lat_key", "_lon_key"])
        .reset_index(drop=True)
    )
    points["_weather_point_id"] = np.arange(len(points))

    w = w.merge(points[["_lat_key", "_lon_key", "_weather_point_id"]],
                on=["_lat_key", "_lon_key"], how="left")

    # If the same location/hour was ingested more than once, keep the newest copy
    if "ingested_at_utc" in w.columns:
        w = w.sort_values("ingested_at_utc")
    w = w.drop_duplicates(subset=["_weather_point_id", "_hour_utc"], keep="last")

    drop = ["observed_at_utc", "ingested_at_utc", "_lat_key", "_lon_key"]
    w = w.drop(columns=[c for c in drop if c in w.columns])
    w = w.rename(columns={
        "location": "weather_location",
        "latitude": "weather_latitude",
        "longitude": "weather_longitude",
        "observed_at_local": "weather_observed_at_local",
    })
    return points, w


#Main combine function
def build_combined(
    delays: pd.DataFrame,
    station_data: pd.DataFrame,
    weather_hourly: pd.DataFrame,
    ridership: pd.DataFrame,
    ridership_naive_tz: str = RIDERSHIP_NAIVE_TZ,
) -> pd.DataFrame:
    n_delays = len(delays)

    d = prep_delays(delays)
    s = prep_stations(station_data)
    r = prep_ridership(ridership, ridership_naive_tz)
    points, w = prep_weather(weather_hourly)

    # 1) delays + station  (delays.parent_station = station_data.stop_id)
    out = d.merge(s, left_on="parent_station", right_on="_station_key",
                  how="left", validate="many_to_one").drop(columns="_station_key")

    # 2) + ridership  (parent_station, hour_utc) = (stop_id, datetime)
    out = out.merge(r, left_on=["parent_station", "_hour_utc"],
                    right_on=["_station_key", "_hour_utc"],
                    how="left", validate="many_to_one").drop(columns="_station_key")

    # 3) nearest weather location for each station that has coordinates
    stations_xy = (
        out[["parent_station", "station_stop_lat", "station_stop_lon"]]
        .drop_duplicates("parent_station")
        .dropna(subset=["station_stop_lat", "station_stop_lon"])
    )
    idx, dist_km = nearest_points(
        stations_xy["station_stop_lat"].to_numpy(),
        stations_xy["station_stop_lon"].to_numpy(),
        points["_lat_key"].to_numpy(),
        points["_lon_key"].to_numpy(),
    )
    station_to_weather = pd.DataFrame({
        "parent_station": stations_xy["parent_station"].to_numpy(),
        "_weather_point_id": points["_weather_point_id"].to_numpy()[idx],
        "weather_distance_km": dist_km,
    })
    out = out.merge(station_to_weather, on="parent_station",
                    how="left", validate="many_to_one")

    # 4) + weather for that location in the same hour
    out = out.merge(w, on=["_weather_point_id", "_hour_utc"],
                    how="left", validate="many_to_one")

    # Fix some things up: keep the parsed UTC hour as a real datetime, remove helper keys
    out["hour_utc"] = out["_hour_utc"]
    out = out.drop(columns=["_hour_utc", "_weather_point_id"])

    assert len(out) == n_delays, f"Row count changed: {n_delays} -> {len(out)}"

    def pct(mask):
        return f"{mask.mean():.1%}"
    print(f"Rows: {len(out):,} (delays had {n_delays:,})")
    print(f"  matched to station:   {pct(out['station_stop_name'].notna())}")
    print(f"  matched to ridership: {pct(out['hourly_entries'].notna())}")
    print(f"  matched to weather:   {pct(out['weather_location'].notna())}")
    print(f"  station->weather distance km: "
          f"median {np.median(dist_km):.2f}, max {dist_km.max():.2f}"
          if len(dist_km) else "  no station coordinates found")

    unmatched = sorted(set(d["parent_station"]) - set(s["_station_key"]))
    if unmatched:
        print(f"  {len(unmatched)} delay stations not in station_data, e.g. {unmatched[:5]}")
    if out["hourly_entries"].isna().mean() > 0.5:
        print("Daniel Ridership Flag")

    return out


#Load from GCS
def get_bucket():
    #Connect to the bucket using the IDs in user_definition.py.
    from google.cloud import storage
    from fastapi_functions.user_definition import project_id, bucket_name, service_account_file_path

    client = storage.Client.from_service_account_json(
        service_account_file_path, project=project_id
    )
    return client.bucket(bucket_name)


def all_panels(blob_names):
    panels = []
    for name in blob_names:
        m = _PANEL_RE.search(name)
        if not m:
            continue
        end = pd.Timestamp(m.group(2) or m.group(1))
        panels.append((end, name))
    if not panels:
        raise FileNotFoundError(f"No files matching {_PANEL_RE.pattern} under {DELAYS_PREFIX}")
    panels.sort(key=lambda p: p[0].value)
    return [name for _, name in panels]


def pick_latest_panel(blob_names):
    panels = []
    for name in blob_names:
        m = _PANEL_RE.search(name)
        if not m:
            continue
        start = pd.Timestamp(m.group(1))
        end = pd.Timestamp(m.group(2) or m.group(1))
        panels.append((end, start, name))
    if not panels:
        raise FileNotFoundError(f"No files matching {_PANEL_RE.pattern} under {DELAYS_PREFIX}")
    # Latest end date first; on a tie, earliest start date first
    panels.sort(key=lambda p: (-p[0].value, p[1].value))
    return panels[0][2]


def resolve_blob_name(bucket, name):
    if bucket.blob(name).exists():
        return name
    candidates = [b.name for b in bucket.list_blobs(prefix=name) if not b.name.endswith("/")]
    preferred = [c for c in candidates if c.endswith((".csv", ".parquet"))]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f"gs://{bucket.name}/{name} not found")
    raise FileNotFoundError(f"Several objects start with {name}: {candidates}")


def read_blob(bucket, name, **read_kwargs):
    data = bucket.blob(name).download_as_bytes()
    if name.endswith(".parquet"):
        return pd.read_parquet(io.BytesIO(data))
    return pd.read_csv(io.BytesIO(data), **read_kwargs)


def load_sources_from_gcs(bucket=None, station_blob = STATION_BLOB, sources = None):
    bucket = bucket or get_bucket()

    station_name = resolve_blob_name(bucket, station_blob)
    panel_names = all_panels(b.name for b in bucket.list_blobs(prefix=DELAYS_PREFIX))
    weather_names = [WEATHER_BLOB] + sorted(
        b.name for b in bucket.list_blobs(prefix=WEATHER_DAILY_PREFIX)
        if b.name.endswith(".parquet"))

    def load(label, name, **kwargs):
        df = read_blob(bucket, name, **kwargs)
        if sources is not None:
            sources[label] = name
        print(f"  {label:<10} gs://{bucket.name}/{name}  ({len(df):,} rows)")
        return df

    def load_many(label, names, dedupe_on, **kwargs):
        frames = []
        for name in names:
            try:
                frames.append(read_blob(bucket, name, **kwargs))
            except Exception as exc:
                print(f"  {label:<10} skipped {name}: {type(exc).__name__}: {exc}")
        if not frames:
            raise FileNotFoundError(f"No readable {label} objects: {names}")
        df = pd.concat(frames, ignore_index=True)
        before = len(df)
        if dedupe_on and all(c in df.columns for c in dedupe_on):
            df = df.drop_duplicates(subset=dedupe_on, keep="last").reset_index(drop=True)
        if sources is not None:
            sources[label] = f"{len(frames)} object(s), latest {names[-1]}"
        print(f"  {label:<10} {len(frames)} object(s)  ({before:,} rows -> "
              f"{len(df):,} after dedupe)")
        return df

    print("Loaded files from GCS:")
    station_data = load("station", station_name, low_memory=False,
                        dtype={"stop_id": str, "parent_station": str})
    weather_hourly = load_many("weather", weather_names, dedupe_on=None)
    ridership = load("ridership", RIDERSHIP_BLOB, dtype={"stop_id": str})
    # Panels overlap; the one with the later end date was computed later.
    delays = load_many("delays", panel_names,
                       dedupe_on=["parent_station", "hour_utc"],
                       dtype={"parent_station": str})
    return delays, station_data, weather_hourly, ridership


def upload_csv(bucket, df, name=COMBINED_BLOB):
    bucket.blob(name).upload_from_string(df.to_csv(index=False), content_type="text/csv")
    print(f"Uploaded {len(df):,} rows to gs://{bucket.name}/{name}")


def upload_parquet(bucket, df, name=COMBINED_PARQUET_BLOB):
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    bucket.blob(name).upload_from_string(
        buffer.getvalue(), content_type="application/vnd.apache.parquet")
    print(f"Uploaded {len(df):,} rows to gs://{bucket.name}/{name} "
          f"({buffer.getbuffer().nbytes / 1e6:,.0f} MB)")


if __name__ == "__main__":
    bucket = get_bucket()
    combined = build_combined(*load_sources_from_gcs(bucket))
    print(combined.head())
    upload_csv(bucket, combined)
    upload_parquet(bucket, combined)