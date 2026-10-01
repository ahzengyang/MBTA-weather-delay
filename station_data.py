"""
Python script to download MBTA GTFS feed, extract only subway
station file, and upload to GCS. 

Run directly:

    python scrape_stations.py

Config info (URLs, bucket name) comes from user_definition.py.
"""
import csv
import io
import zipfile

import requests
from google.cloud import storage
from google.oauth2 import service_account


from user_definition import(
    mbta_gtfs_urlL,
    project_id,
    bucket_name,
    service_account_file_path,
    station_data_file_name
) 


def get_subway_stations_csv(url: str) -> bytes:
    response = requests.get(url, timeout=30)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as z, z.open("stops.txt") as f:
        reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
        header = next(reader)
        rows = list(reader)

    index = {name: i for i, name in enumerate(header)}
    subway_parent_ids = {
        row[index["parent_station"]]
        for row in rows
        if row[index["location_type"]] == "0" and row[index["vehicle_type"]] in ("0", "1")
    }
    station_rows = [row for row in rows if row[index["stop_id"]] in subway_parent_ids]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(station_rows)
    return buf.getvalue().encode("utf-8")

def upload_to_gcs(data: bytes, bucket_name: str, blob_path: str):
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file_path)
    client = storage.Client(project_id, credentials)
    blob = client.bucket(bucket_name).blob(blob_path)
    blob.upload_from_string(data, content_type="text/csv")


if __name__ == "__main__":
    csv_bytes = get_subway_stations_csv(mbta_gtfs_urlL)
    upload_to_gcs(csv_bytes, bucket_name, station_data_file_name)
    print(f"Uploaded to gs://{bucket_name}/{station_data_file_name}")
