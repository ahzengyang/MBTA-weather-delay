import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
project_id = os.getenv('GCP_PROJECT_ID')
bucket_name = os.getenv('GCP_BUCKET_NAME')
service_account_file_path = os.getenv('GCP_SERVICE_ACCOUNT_KEY')

station_data_file_name = 'station_raw/station_data_file'
mbta_gtfs_url="https://cdn.mbta.com/MBTA_GTFS.zip"