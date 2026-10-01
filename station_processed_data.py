"""
Python script to take raw subway station file from GCS, transform
it for our analysis and upload to GCS. 

Run directly:

    python station_processed_data.py

Config info (URLs, bucket name) comes from user_definition.py.
"""
import io
import zipfile
import pandas as pd
import requests

from google.cloud import storage
from google.oauth2 import service_account

from user_definition import(
    project_id,
    bucket_name,
    service_account_file_path,
    station_data_file_name
) 

def process_subway_stations(raw_csv_file: str) -> bytes:
    
    # with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
    #     raw_csv_bytes= z.read("stops.txt")

    # reader = csv.reader(io.TextIOWrapper(io.BytesIO(raw_csv_bytes), encoding="utf-8"))
    # header = next(reader)
    # rows = list(reader)  # every field stays a plain string, nothing inferred

    # idx = {name: i for i, name in enumerate(header)}
    # subway_parent_ids = {
    #     row[idx["parent_station"]]
    #     for row in rows
    #     if row[idx["location_type"]] == "0" and row[idx["vehicle_type"]] in ("0", "1")
    # }
    # station_rows = [row for row in rows if row[idx["stop_id"]] in subway_parent_ids]

    # buf = io.StringIO()
    # writer = csv.writer(buf)
    # writer.writerow(header)
    # writer.writerows(station_rows)
    # return buf.getvalue().encode("utf-8")

def upload_to_gcs(data: bytes, bucket_name: str, blob_path: str):
    try:
        credentials = service_account.Credentials.from_service_account_file(
            service_account_file_path)
        client = storage.Client(project_id, credentials)
        blob = client.bucket(bucket_name).blob(blob_path)
        blob.upload_from_string(data, content_type="text/csv")

        print(f"Uploaded to gs://{bucket_name}/{station_data_file_name}")
    except Exception as e:
        print(f"Error uploading to cloud: {e}")
        raise


if __name__ == "__main__":
    data = process_subway_stations()
    upload_to_gcs(data, bucket_name, station_data_file_name)
    
