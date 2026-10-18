# Massachusetts Bay Transportation Authority Weather Delays Since June 1st, 2025

We are looking to see if major weather problems caused delays within the subway system for the MBTA. We are taking data sources of Delays, Weather, Stations, and Number of Passengers for the MBTA since June 1st, 2025 in order to find possible correlations with weather on delays and number of people arriving at stations on an hourly basis.

**Live web app:** `https://mbta-weather-dashboard-207469279179.us-west1.run.app/`
**API (private):** `https://mbta-weather-api-207469279179.us-west1.run.app`

## Team Members

| Name           | GitHubID        | Role / Focus                        |
| -------------- | --------------- | ----------------------------------- |
| Alex Casella   | Alex-Casella    | Weather API (source 2), combine API files             |
| Daniel Patel   | danielpatel2000 | Delays API (source 1), combine data sources               |
| Aditya Verma   | a6itya          | Number of passengers API (source 3), report |
| Angela Wei     | angelaw6        | MBTA GTFS datasets (source 4), streamlit       |
| Alex Zeng-Yang | ahzengyang      | Delays API (source 1), cloud run and scheduler               |

---

## Problem Statement

We asked the question of whether MBTA trains arrive later during rainy and high-wind conditions. We also want an aesthetically pleasing dashboard that viewers can interact with to learn about the train delay trends in the Boston area. We combined the following datasets: 1) MBTA LAMP real-time scheduled and delayed arrival times, 2) Open-Meteo weather data, 3) MBTA daily station exits to determine the number of passengers affected by delays, 4) Wikipedia Massachusetts Bay Transit Stations to determine the location of the stations. We created a dashboard mapping all MBTA stations, sized by average delay, where each station on the map can be clicked to see delay and weather information and trends for that specific station. The dashboard also has interactive visualizations that show how rush hour and off-peak times affect delay, how different weather events like snow and rain affect delay, and which stations have the worst delay on a weekly basis

---

## Data Sources and Integration Goal

### Sources

| #   | Source & Link                                                                              | Method            | What it contains                                                                                                                                                                                                                    | Update frequency       | Access requirements |
| --- | ------------------------------------------------------------------------------------------ | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- | ------------------- |
| 1   | [MBTA Delays](https://performancedata.mbta.com/lamp/subway-on-time-performance-v1)         | API               | One row for each hour and each station, containing the average delay of the trains within that hour                                                                                                                                 | Daily                  | none                |
| 2   | [Weather](https://open-meteo.com/en/docs/gfs-api)                                          | API               | Hourly weather for each MBTA station (above ground specifically) based on longitude and latitude. Includes measures of temperature, precipitation (either rain OR snow), wind speed/gusts, cloud coverage, and WMO weather code.    | Daily                 | none                |
| 3   | [Subway Ridership](https://gis.data.mass.gov/datasets/MassDOT::mbta-gated-station-entries) | Website Download  | The amount of entries for each gated subway station by the hour                                                                                                                                                                     | Monthly                | none                |
| 4   | [Subway Stations](https://www.mbta.com/developers/gtfs)                                    | TXT File Download | Bulk schedule data in General Transit Feed Specification format about MTBA system and service. Multiple datasets which include stops.txt detailing stop id, name, longitude, latitude, searchable address, platform name, and more. | Yearly | None                |


### Integration Goal

MBTA Delays Dataset supplies the outcome of schedule deviation per train per station per minute. Weather Dataset supplies the weather at a station at a given time. Ridership Dataset supplies the weight of how many people were impacted by the delay instead of just looking at median delay. Stations Dataset tells us whether the station is under or above ground, and the latitude and longitude of each station so it can be joined to weather data.

<ins>Subway Stations Dataset</ins>  
Join keys: parent_station, latitude, longitude

<ins>MBTA Delays Dataset</ins>  
Join keys: parent_station, hour_utc

<ins>Weather Dataset</ins>  
Join keys: latitude, longitude

<ins>Subway Ridership Dataset</ins>  
Join keys: parent_station, utc_hour

---

## Repository Structure

```
MBTA-weather-delay/
├── fastapi/                          # FastAPI collection service (Cloud Run)
│   ├── fastapi_functions/
│   │   ├── __init__.py
│   │   ├── gcs.py                    # GCS read/write helpers for the ridership chain
│   │   ├── gse_clean.py              # 30-min rows → hourly entries per station
│   │   ├── gse_fetch.py              # Source 1: gated station entries, ArcGIS API
│   │   ├── run_pipeline.py           # Ridership ETL: fetch → clean → upload
│   │   ├── source1_mbta_lamp_delay.py # Source 2: MBTA LAMP delay panel
│   │   ├── station_data.py           # Source 3: GTFS stops + levels, is_underground
│   │   ├── stations.py               # The 69 gated station IDs
│   │   ├── user_definition.py        # Shared config and .env loading
│   │   └── weather_openmeteo.py      # Source 4: Open-Meteo hourly weather
|   ├── .dockerignore
│   ├── Dockerfile
│   ├── combine_sources.py            # Joins all four sources into one table
│   ├── mbta_weather_api.py           # FastAPI routes
│   └── requirements.txt
├── streamlit/                        # Streamlit dashboard (Cloud Run)
│   ├── .streamlit/
│   │   ├── config.toml
|   ├── .dockerignore
│   ├── Dockerfile
│   ├── app.py                        # Entry point: loads data, filters, routing
│   ├── overview.py                   # Weekly overview page
│   ├── requirements.txt
│   ├── station_file.py               # Per-station detail page
│   ├── station_map.py                # pydeck station map
│   ├── user_definition.py            # Config (separate build context)
│   └── weather_eda.py                # Weather vs delay analysis + palette
├── .env_template
├── .gitignore
├── gcloud_command.sh                 # Deploys both services and all scheduler jobs
└── README.md
```

---

## API Endpoints

| Method | Path | Purpose | Example |
| --- | --- | --- | --- |
| `GET` | `/health` | Checks API is live | `/health` |
| `GET` | `/stations` | Gets MBTA subway station table | `/stations?format=csv` |
| `POST` | `/stations/upload` | Builds the station table and write it to GCS | `/stations/upload?blob_path=stations/mbta_subway_stations.csv` |
| `GET` | `/delays/dates` | List of service dates uploaded to LAMP's index.csv | `/delays/dates` |
| `GET` | `/delays/panel` | station × hour delay panel for date range | `/delays/panel?start_date=2025-01-01&end_date=2025-03-31&format=json` |
| `POST` | `/delays/panel/upload` | build the delay panel and write it to GCS  | `/delays/panel/upload?start_date=2025-08-01&end_date=2025-08-04` |
| `GET` | `/weather` | fetch hourly Open-Meteo weather for one station | `/weather?station=Park Street&start=2025-03-01&end=2025-03-07` |
| `POST` | `/weather/refresh` | fetch hourly weather for every station | `/weather/refresh?start=2025-08-01&end=2025-08-10` |
| `POST` | `/weather/daily` | fetch one day of weather for every station and write to GCS | `/weather/daily?day=2026-09-15` |
| `POST` | `/ridership/refresh` | rerun whole ridership pipeline and uplaod to GCS | `/ridership/refresh?start=2025-01-01&end=2025-12-31` |
| `GET` | `/ridership/hourly-all` | Hourly gated entries per station. Filter by date, station, and line | `/ridership/hourly-all?date=2025-03-14&line=Red` |
| `GET` | `/ridership/raw` | Raw 30-minute ridership rows for one service date | `/ridership/raw?date=2025-03-14&station=Harvard` |
| `POST` | `/combined/refresh` | Join all the source's data from GCS and write back to GCS | `/combined/refresh` |

---


## Setup Instructions (Locally)

### Prerequisites

- Python 3.11+, Docker Desktop, `gcloud` CLI
- A GCP service account key with access to GCP bucket: mbta-weather-delay/mbta-weather-delay

### 1. Clone the repository

```bash
git clone https://github.com/ahzengyang/MBTA-weather-delay.git
cd MBTA-weather-delay
```

### 2. Configure environment variables

Copy the example file and fill in your own values:

```bash
cp .env_template .env
```

| Variable                  | Description                                | Example                    |
| ------------------------- | ------------------------------------------ | -------------------------- |
| `GCP_PROJECT_ID`          | ID of GCP project                          | `project_id`               |
| `GCP_SERVICE_ACCOUNT_KEY` | Absolute path to your service account JSON | `/Users/you/.ssh/key.json` |
| `GCP_BUCKET_NAME`         | Name of GCP bucket                         | `bucket_name`              |
| `GCS_PREFIX`              | Name of GCP bucket folder                  | `mbta_gse/processed`       |
| `GCS_RAW_PREFIX`          | Name of GCP bucket folder                  | `mbta_gse/raw`             |

### 3. Run both services

On api folder,

```bash
docker build -t api .
```

On streamlit folder,

```bash
docker build -t streamlit .
```

- API docs: http://localhost:8000/docs
- Web app: http://localhost:8501

### 4. Trigger a collection run

```bash
curl -X POST http://localhost:8000/delays/panel/upload -H "Content-Type: application/json" -d '{}'
```

Then confirm the object landed in your bucket.

---

## Setup — Deploy to GCP

Copy the example file and edit the CONFIG block at the top of `gcloud_command.sh`, then:

```bash
cp gcloud_command_template.sh gcloud_command.sh
```

Edit the CONFIG block at the top of `gcloud_command.sh`, then:

```bash
./gcloud_command.sh all
```

Or one piece at a time while iterating:

```bash
./gcloud_command.sh bootstrap   # enable APIs, create Artifact Registry repo
./gcloud_command.sh api         # rebuild + redeploy the API
./gcloud_command.sh web         # rebuild + redeploy the web app
./gcloud_command.sh scheduler   # (re)create the Cloud Scheduler job
./gcloud_command.sh urls        # print both service URLs
```

---

