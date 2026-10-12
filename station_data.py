"""
Python script to download MBTA GTFS feed, extract datasets about subway stations (stops.txt and levels.txt),
data cleaning and transforming, then upload to GCS as CSV file format.

Run directly:

    python station_data.py

Config info (URLs, GCS info) comes from user_definition.py.
FIX FUNCTIONS
"""

import io
import zipfile
import pandas as pd
import requests
import csv

from google.cloud import storage
from google.oauth2 import service_account

from user_definition import(
    mbta_gtfs_url,
    project_id,
    bucket_name,
    service_account_file_path,
    station_data_file_name,
    level_data_file_name
)

"""
Function to extract raw TXT file of 'file' name from MTBA GFS feed
"""
def get_raw_csv(url: str, file: str):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z, z.open(file) as f:
        df = pd.read_csv(f, dtype=str, na_filter=False)
    return df# .to_csv(index=False).encode("utf-8")

"""
Function to upload data file to GCS bucket, from bytes to csv file format ??
Change - upload data file to gcs bucket from pandas df to csv file format
"""
def upload_to_gcs(data: bytes, bucket_name: str, blob_path: str):
    try:
        credentials = service_account.Credentials.from_service_account_file(
            service_account_file_path)
        client = storage.Client(project_id, credentials)
        blob = client.bucket(bucket_name).blob(blob_path)
        blob.upload_from_string(data, content_type="text/csv")
        print(f"Uploaded to gs://{bucket_name}/{blob_path}")
    except Exception as e:
        print(f"Error uploading to cloud: {e}")
        raise

"""
Function to process stops station data, collapsing rows into unique stations 
"""
def process_subway_stations(service_account_file_path: str, project_id: str, bucket_name: str, blob_path:str):
    # Acquire raw data csv from GCS
    credentials = service_account.Credentials.from_service_account_file(
                service_account_file_path)
    client = storage.Client(project_id, credentials)
    bucket = client.bucket(bucket_name)
    file = bucket.blob(blob_path)
    raw_csv_bytes = file.download_as_bytes()

    # Transform: collapse platform-level rows into one row per subway station
    reader = csv.reader(io.TextIOWrapper(io.BytesIO(raw_csv_bytes), encoding="utf-8"))
    header = next(reader)
    rows = list(reader)  # every field stays a plain string, nothing inferred

    idx = {name: i for i, name in enumerate(header)}
    subway_parent_ids = {
        row[idx["parent_station"]]
        for row in rows
        if row[idx["location_type"]] == "0" and row[idx["vehicle_type"]] in ("0", "1")
    }
    station_rows = [row for row in rows if row[idx["stop_id"]] in subway_parent_ids]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(station_rows)
    return buf.getvalue().encode("utf-8")

"""
Function to upload data file to GCS bucket, from bytes to csv file format 
"""
def get_underground_flags(stops_df: pd.DataFrame, levels_df: pd.DataFrame) -> pd.Series:
    platforms = stops_df[
        (stops_df["location_type"] == "0") & (stops_df["vehicle_type"].isin(["0", "1"]))
    ]

    level_index_by_id = levels_df.set_index("level_id")["level_index"].astype(float)
    platform_levels = platforms["level_id"].map(level_index_by_id)

    is_underground = (
        platform_levels.lt(0)
        .groupby(platforms["parent_station"])
        .any()
        .map({True: "1", False: "0"})
    )
    return is_underground  # Series indexed by station_id, values "1"/"0"



def filter_station_variables(station_data_bytes: bytes):

    df = pd.read_csv(io.BytesIO(station_data_bytes), dtype=str, na_filter=False)
    keep_columns = [
        "stop_id",
        "stop_name",
        "stop_lat",
        "stop_lon",
        "municipality",
        "stop_address",
        "stop_url",
        "is_underground"
    ]
    return df[keep_columns]

if __name__ == "__main__":
    """
    - Extract raw data
    - Process stops.txt, collapse rows into unique stations
    - Process levels.txt to extract us_underground flag
    - Left join stops and levels for final processed dataset
    """
    
    stops = get_raw_csv(mbta_gtfs_url, "stops.txt")
    levels = get_raw_csv(mbta_gtfs_url, "levels.txt")

    data_bytes = process_subway_stations(service_account_file_path, project_id, bucket_name, f"station_raw/{station_data_file_name}")
    processed_stops_df = pd.read_csv(io.BytesIO(data_bytes), dtype=str, na_filter=False)

    underground_flags = get_underground_flags(stops, levels)
    processed_stops_df["is_underground"] = processed_stops_df["stop_id"].map(underground_flags).fillna("0")

    processed_stops_df = filter_station_variables(processed_stops_df.to_csv(index=False).encode("utf-8"))
    """
    Upload processed file into gcs
    """

    final_data_bytes = processed_stops_df.to_csv(index=False).encode("utf-8")
    upload_to_gcs(final_data_bytes, bucket_name, f"station_processed/{station_data_file_name}_final2")

    """ Other """
    #data = filter_station_variables(data_bytes)
    #print(get_underground_flags(stops, levels))
    #upload_to_gcs(stops.to_csv(index=False).encode("utf-8"), bucket_name, f"station_raw/{station_data_file_name}")
    #upload_to_gcs(levels.to_csv(index=False).encode("utf-8"), bucket_name, f"station_raw/{level_data_file_name}")
    #print(processed_stops_df)
    
        


