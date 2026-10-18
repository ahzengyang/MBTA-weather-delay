# Massachusetts Bay Transportation Authority Weather Delays Since June 1st, 2025

We are looking to see if major weather problems caused delays within the subway system for the MBTA. We are taking data sources of Delays, Weather, Stations, and Number of Passengers for the MBTA since June 1st, 2025 in order to find possible correlations with weather on delays and number of people arriving at stations on an hourly basis.

## Team Members

| Name           | GitHubID        | Role / Focus                        |
| -------------- | --------------- | ----------------------------------- |
| Alex Casella   | Alex-Casella    | Weather API (source 2)              |
| Daniel Patel   | danielpatel2000 | Delays API (source 1)               |
| Aditya Verma   | a6itya          | Number of passengers API (source 3) |
| Angela Wei     | angelaw6        | MBTA GTFS datasets (source 4)       |
| Alex Zeng-Yang | ahzengyang      | Delays API (source 1)               |

---

## Problem Statement

- We ask the question of whether MBTA trains arrive later during rainy and high-wind conditions, and whether this pattern is only seen at the 34 stations where the track and platform are above ground rather than the 16 underground stations. We will combine the following datasets to answer this question: 1) MBTA scheduled and predicted arrival times, 2) Open-Mateo weather data, 3) MBTA daily station exits to determine the number of passengers affected by delays, 4) Wikipedia Massachusetts Bay Transit Stations to determine whether the station is underground or exposed to the elements. We plan to make a dashboard which matches all 52 MBTA stations. We will emphasize this with a precipitation vs delay scatter plot that can be changed between underground and exposed stations as well as rain versus snow. We plan to have a filter on the dashboard that allows you to select filters for line, county, hour of the day, and the date. This could be useful to the MBTA operations group who decide how, where, and when to place buffer into the train schedule and can be used to improve their logistics system.

---

## Data Sources and Integration Goal

### Sources

| #   | Source & Link                                                                              | Method            | What it contains                                                                                                                                                                                                                    | Update frequency       | Access requirements |
| --- | ------------------------------------------------------------------------------------------ | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- | ------------------- |
| 1   | [MBTA Delays](https://performancedata.mbta.com/lamp/subway-on-time-performance-v1)         | API               | One row for each hour and each station, containing the average delay of the trains within that hour                                                                                                                                 | daily                  | none                |
| 2   | [Weather](https://open-meteo.com/en/docs/gfs-api)                                          | API               | Hourly weather for each MBTA station (above ground specifically) based on longitude and latitude. Includes measures of temperature, precipitation (either rain OR snow), wind speed/gusts, cloud coverage, and WMO weather code.    | Hourly                 | none                |
| 3   | [Subway Ridership](https://gis.data.mass.gov/datasets/MassDOT::mbta-gated-station-entries) | Website Download  | The amount of entries for each gated subway station by the hour                                                                                                                                                                     | Monthly                | none                |
| 4   | [Subway Stations](https://www.mbta.com/developers/gtfs)                                    | TXT File Download | Bulk schedule data in General Transit Feed Specification format about MTBA system and service. Multiple datasets which include stops.txt detailing stop id, name, longitude, latitude, searchable address, platform name, and more. | Multiple times a month | None                |

Note: If we need a key, say which environment variable holds it and make sure that variable also appears in the .env_template

### Integration Goal

MBTA Delays Dataset supplies the outcome of schedule deviation per train per station per minute. Weather Dataset supplies the weather at a station at a given time. Ridership Dataset supplies the weight of how many people were impacted by the delay instead of just looking at median delay. Stations Dataset tells us if the station is open to the sky which helps us look at the effect of weather for different classifications of stations.

<ins>Subway Stations Dataset</ins>  
Join keys: parent_station, latitude, longitude

<ins>MBTA Delays Dataset</ins>  
Join keys: parent_station, hour_utc

<ins>Weather Dataset</ins>  
Join keys: lat and longitude to station

<ins>Subway Ridership Dataset</ins>  
Join keys: parent_station, utc_hour

---

## Setup Instructions (Locally)

### Prerequisites

- Python 3.11+, Docker Desktop, `gcloud` CLI
- A GCP service account key with access to mbta-weather-delay/mbta-weather-delay/mbta_lamp/processed/

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
curl -X POST http://localhost:8000/collect -H "Content-Type: application/json" -d '{}'
```

Then confirm the object landed in your bucket.

---

## Setup — Deploy to GCP

Copy the example file and edit the CONFIG block at the top of `gcloud_command.sh`, then:

```bash
cp gcloud_command_template.sh gcloud_command.sh
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
