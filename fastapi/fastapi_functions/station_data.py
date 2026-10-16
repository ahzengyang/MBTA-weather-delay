"""
Python script to download MBTA GTFS feed, extract datasets about subway stations (stops.txt and levels.txt),
data cleaning and transforming, then upload to GCS as CSV file format.

Run directly:
    python station_data.py

Config info (URLs, GCS info) comes from user_definition.py.
"""
import io
import zipfile
import pandas as pd
import requests
import csv

from google.cloud import storage
from google.oauth2 import service_account

from .user_definition import(
    mbta_gtfs_url,
    project_id,
    bucket_name,
    service_account_file_path,
    station_data_file_name
)

"""
Function to extract raw TXT file of 'file' name from MTBA GFS feed
"""
def get_raw_csv(url: str, file: str) -> pd.DataFrame: 
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z, z.open(file) as f:
        df = pd.read_csv(f, dtype=str, na_filter=False)
    return df

"""
Function to upload data file to gcs bucket from pandas df to csv file format
"""
def upload_to_gcs(data: pd.DataFrame, service_account_file_path: str,
                  project_id: str, bucket_name: str, blob_path:str):
    try:
        credentials = service_account.Credentials.from_service_account_file(
            service_account_file_path)
        client = storage.Client(project_id, credentials)
        blob = client.bucket(bucket_name).blob(blob_path)

        blob.upload_from_string(data.to_csv(index=False).encode("utf-8"), content_type="text/csv")
        print(f"Uploaded to gs://{bucket_name}/{blob_path}")
    except Exception as e:
        print(f"Error uploading to cloud: {e}")
        raise

"""
Function to extract is_underground binary flag from both raw datasets 
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
    return is_underground

"""
Data transformation function to process stops station data, collapse rows into unique stations,
join with is_underground flag, and filter out variables  
"""
def process_subway_stations(stops_df: pd.DataFrame, underground_flags: pd.Series):
    # collapse all rows into unique station stops
    is_subway_platform = (
        (stops_df["location_type"] == "0") & (stops_df["vehicle_type"].isin(["0", "1"]))
    )
    subway_parent_ids = set(stops_df.loc[is_subway_platform, "parent_station"])
    output_df = stops_df[stops_df["stop_id"].isin(subway_parent_ids)]

    # left join stops df with is_underground to create new column
    output_df["is_underground"] = output_df["stop_id"].map(underground_flags).fillna("0")

    # create final df by filtering out unnecessary variables
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
    
    return output_df[keep_columns]

if __name__ == "__main__":
    # Data acquisition
    stops = get_raw_csv(mbta_gtfs_url, "stops.txt")
    levels = get_raw_csv(mbta_gtfs_url, "levels.txt")

    # Data transformation
    underground_flags = get_underground_flags(stops, levels)
    processed_stops_df = process_subway_stations(stops, underground_flags)

    # Data upload to gcs
    upload_to_gcs(processed_stops_df, service_account_file_path, project_id,
                  bucket_name, f"station_processed/{station_data_file_name}_final")
        

