"""
Python script to download MBTA GTFS feed, extract only subway
station file, and upload to GCS. 

Run directly:

    python scrape_stations.py

Config info (URLs, bucket name) comes from user_definition.py.
"""
import io
import zipfile
import pandas as pd
import requests

from google.cloud import storage
from google.oauth2 import service_account

from user_definition import(
    mbta_gtfs_url,
    project_id,
    bucket_name,
    service_account_file_path,
    station_data_file_name
) 

def get_raw_stations_csv(url: str):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z, z.open("stops.txt") as f:
        df = pd.read_csv(f, dtype=str, na_filter=False)
    return df.to_csv(index=False).encode("utf-8")

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


if __name__ == "__main__":
    csv_bytes = get_raw_stations_csv(mbta_gtfs_url)
    upload_to_gcs(csv_bytes, bucket_name, station_data_file_name)
    
