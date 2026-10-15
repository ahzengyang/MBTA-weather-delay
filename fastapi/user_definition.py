import os

from dotenv import load_dotenv

load_dotenv()

project_id = os.getenv("GCP_PROJECT_ID")
bucket_name = os.getenv("GCP_BUCKET_NAME")
service_account_file_path = os.getenv("GCP_SERVICE_ACCOUNT_KEY")

mbta_gtfs_url = "https://cdn.mbta.com/MBTA_GTFS.zip"
station_data_file_name = "station_data_file"

mbta_url = "https://api-v3.mbta.com"
