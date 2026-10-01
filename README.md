# Massachusetts Bay Transportation Authority Weather Delays Since 2024
Add a brief description of your project, in a sentence or two.

## Team Members

| Name | GitHubID | Role / Focus |
| --- | --- | --- |
| Alex Zeng-Yang | ahzengyang | Delays API (source 1) |
| Daniel Patel | danielpatel2000 | Delays API (source 1)|
| Alex Casella | Alex-Casella | Weather API (source 2)|
| Aditya Verma | a6itya | Number of passengers API (source 3)|
| Angela Wei | angelaw6 | MBTA station website scrape (source 4) |
---

## Problem Statement
- We ask the question of whether MBTA trains arrive later during rainy and high-wind conditions, and whether this pattern is only seen at the 34 stations where the track and platform are above ground rather than the 16 underground stations. We will combine the following datasets to answer this question: 1) MBTA scheduled and predicted arrival times, 2) Open-Mateo weather data, 3) MBTA daily station exits to determine the number of passengers affected by delays, 4) Wikipedia Massachusetts Bay Transit Stations to determine whether the station is underground or exposed to the elements. We plan to make a dashboard which matches all 52 MBTA stations. We will emphasize this with a precipitation vs delay scatter plot that can be changed between underground and exposed stations as well as rain versus snow. We plan to have a filter on the dashboard that allows you to select filters for line, county, hour of the day, and the date. This could be useful to the MBTA operations group who decide how, where, and when to place buffer into the train schedule and can be used to improve their logistics system.


---

## Data Sources and Integration Goal
- Follow the direction given in the 1st assignment

### Sources
| # | Source & Link | Method | What it contains | Update frequency | Access requirements |
| --- | --- | --- | --- | --- | --- |
| 1 | [MBTA Delays]([https://exact-url](https://performancedata.mbta.com/lamp/subway-on-time-performance-v1)) | API | One row for each hour and each station, containing the average delay of the trains within that hour | none |
| 2 | [Weather]((https://archive-api.open-meteo.com/v1/archive)) | API | Hourly weather for each MBTA station (above ground specifically) based on longitude and latitude. Includes measures of temperature, precipitation (either rain OR snow), wind speed/gusts, cloud coverage, and WMO weather code. | Hourly | none |
| 3 | [Subway Ridership]((https://gis.data.mass.gov/datasets/MassDOT::mbta-gated-station-entries)) | Scraped | ... | ... | `robots.txt` checked DATE |
| 4 | [Subway Stations](https://www.mbta.com/developers/gtfs) | TXT File Download | Bulk schedule data in General Transit Feed Specification format about MTBA system and service. Multiple datasets which include stops.txt detailing stop id, name, longitude, latitude, searchable address, platform name, and more.  | Multiple times a month | None |

Note: If we need a key, say which environment variable holds it and make sure that variable also appears in the .env_template

### Integration Goal
- Follow the direction given in the 1st assignment

---

## Setup Instructions (Locally)

### Prerequisites
- Python 3.11+
- A GCP service account key with access to PROJECT/BUCKET/DATASET
- Any source API keys listed in the table below

### 1. Clone the repository
```bash
git clone https://github.com/ORG/REPO.git
cd REPO
```

### 2. Configure environment variables
Copy the example file and fill in your own values:

```bash
cp .env_template .env
```

| Variable | Description | Example |
| --- | --- | --- |
| `GCP_SERVICE_ACCOUNT_KEY` | Absolute path to your service account JSON | `/Users/you/.ssh/key.json` |
| `SOURCE_API_KEY` | Key for SOURCE NAME (free tier) | `abc123...` |
| `API_SERVICE_URL` | Where the web app reaches the API | `http://api-server:8000` |

### 4. How to call your endpoint
To start the API server,
```python
fastapi run mycode.py
```

```python
requests.post("http://localhost:8000/something", json=something)
```
Make sure it writes the data in the bucket.

---
## Repository Structure
```
.
├── your_code.py
├── .env_template
└── README.md
```
