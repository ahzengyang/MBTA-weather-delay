# ADD YOUR PROJECT TITLE
Add a brief description of your project, in a sentence or two.

## Team Members

| Name | GitHubID | Role / Focus |
| --- | --- | --- |
| Angela Wei | id | MBTA station website scrape (source 4) |
| Alex Zeng-Yang | id | Delays API (source 1) |
| Aditya Verma | id | Number of passengers API (source 3) |
| Daniel Patel | id | Delays API (source 1)|
| Alex Casella | id | Weather API (source 2)|
---

## Problem Statement
- We ask the question of whether MBTA trains arrive later during rainy and high-wind conditions, and whether this pattern is only seen at the 34 stations where the track and platform are above ground rather than the 16 underground stations. We will combine the following datasets to answer this question: 1) 511 SF Bay real-time scheduled and predicted arrival times, 2) Open-Mateo weather data, 3) MBTA daily station exits to determine the number of passengers affected by delays, 4) Wikipedia Massachusetts Bay Transit Stations to determine whether the station is underground or exposed to the elements. We will create a dashboard mapping all 50 MBTA stations, sized by rider-minutes delay, with a precipitation vs delay scatter plot that can be toggled between underground and exposed stations, with the ability to filter by line, county, hour of day, and date range. This could be useful to the MBTA operations group who decide how, where, and when to place buffer into the train schedule.


---

## Data Sources and Integration Goal
- Follow the direction given in the 1st assignment

### Sources
| # | Source & Link | Method | What it contains | Update frequency | Access requirements |
| --- | --- | --- | --- | --- | --- |
| 1 | [NAME](https://exact-url) | API | rows, columns, time range, geography — in your own words | daily / monthly / static | free key, 100 req/day |
| 2 | [NAME](https://exact-url) | File | ... | ... | none |
| 3 | [NAME](https://exact-url) | Scraped | ... | ... | `robots.txt` checked DATE |

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
