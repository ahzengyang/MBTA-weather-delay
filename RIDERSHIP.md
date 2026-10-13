Will need access to the `mbta-weather-delay` bucket. If you don't use a key file, log in once with `gcloud auth application-default login`. To see what's in the bucket now, run the `gcloud storage ls` command in Step 4.

Run a full refresh beforehand so the demo doesn't sit waiting on a download:

```bash
cd ~/Desktop/USF/Fall/data_acq/mbta && source .venv/bin/activate
python run_pipeline.py --upload
```

This pulls the last 365 days from MBTA's live ArcGIS API. It takes a few minutes. If you're short on time, `python run_pipeline.py --local GSE.csv --upload` takes seconds, but only goes up to the date `GSE.csv` was downloaded.

The live demo

Step 1. Start the API.

```bash
fastapi dev api.py
```

Step 2. Open the docs page. Go to http://127.0.0.1:8000/docs. FastAPI builds this page for you: every endpoint has a "Try it out" button, so you can demo without writing any code.

Step 3. Show the two GET endpoints, raw first.

| Call | What to say |
|---|---|
| `/ridership/raw?date=2026-02-23&station=Airport` | "This is exactly what MBTA publishes: 30-minute gate counts." |
| `/ridership/hourly-all?date=2026-02-23&station=Airport` | "We clean it into hourly counts per station. This is the table you join with delays and weather." |

Step 4. Show the bucket. Open the GCS console, or run:

```bash
gcloud storage ls "gs://mbta-weather-delay/mbta_gse/**"
```

Don't click `POST /ridership/refresh` during the demo. It re-runs the whole pipeline and takes minutes. Just explain what it does.

---

This shows how many riders entered each station each hour.

The data source

https://mbta-massdot.opendata.arcgis.com/datasets/001c177f07594e7c99f193dde32284c9

- 2 years of data
- Entries only, MBTA doesn't publish exits
- Public, no API key

API endpoint:

https://services1.arcgis.com/ceiitspzDAHrdGO1/arcgis/rest/services/GSE/FeatureServer/0/query

The output: GSE_hourly_all.csv

| Column | Example | Meaning |
|---|---|---|
| `datetime` | `2026-02-23 08:00:00` | Start of the hour, Boston time |
| `stop_id` | `place-dwnxg` | MBTA parent station ID. **Join on this.** |
| `station_name` | `Downtown Crossing` | |
| `lines` | `Orange Line, Red Line` | Every line at the station |
| `hourly_entries` | `1520.0` | Gate entries in that hour, all lines combined |

Files at a glance:

- stations.py: The 69 station IDs we keep
- gse_fetch.py: 1: download raw data
- gse_clean.py: 2: clean, then build hourly totals
- run_pipeline.py: Runs steps 1–2 and saves the results
- gcs.py: Uploads to and reads from the GCS bucket
- api.py: FastAPI app that runs the pipeline and serves results
- GSE_hourly_all.csv: hourly entries per station, every day
- requirements.txt: Python libraries
- .env_template: Settings to copy into .env

How the pipeline works

```
gse_fetch ──► gse_clean ──► gcs.py ──► GCS bucket
└──── run_pipeline.py runs all of these ────┘

api.py = the same pipeline, triggered over the web, plus endpoints to read results
```

Calls, in order

```
1. gse_fetch.available_range()      first and last date the data covers
2. date window                      the last 365 days, or --start/--end
3. gse_fetch.load()                 one download for all days (~1.1M rows, 71 stations)
4. gse_clean.hourly()               keep 69 stations, drop duplicate rows, fix
                                    timestamps, merge transfer stations, sum
                                    into one row per station per hour
```

stations.py:
- 69 station IDs, e.g. `place-aport` (Airport).
- These are the stations found in **both** the GSE data (71) and the team's
  source 4 station file (125). The two left out, Courthouse and World Trade
  Center, are Silver Line bus stops, not subway.
- **Join on `stop_id`, not names.** Names differ across sources ("State
  Street" vs "State").

gse_fetch.py:
- `load(dates)` downloads raw rows for any list of dates. Pass
  `local="GSE.csv"` to read a downloaded file instead, which is much faster.
- `date_range(start, end)` lists every day between two dates.
- Converts times to Boston time, including daylight saving.
- MBTA days run **3am to 3am**, so for each date it also downloads the
  day before.

gse_clean.py:
- `hourly()` turns raw 30-minute rows into one row per station per hour.
- Transfer stations (e.g. Park St Red + Green) are merged into one row.

run_pipeline.py:
- `run()` holds the steps in "Calls, in order" above.
- `save_raw()` uploads raw data to GCS, one CSV per month.
- Command line options: `--start` / `--end` pick the dates; `--local GSE.csv`
  reads a downloaded file; `--upload` also sends the results to GCS.

gcs.py:
- `save_csv()` and `read_csv()` upload and download CSVs.
- Settings come from `.env`. With no key file, it uses your `gcloud` login.

api.py:
- Has no processing logic of its own. It calls the same `run()`.
- `POST /ridership/refresh` runs the pipeline and uploads the results.
  Optional `start` / `end` query params.
- `GET /ridership/raw` and `/ridership/hourly-all` read results from the bucket.
  Both filter by `date`, `station` (name or `stop_id`) and `line`.

run_pipeline.py vs api.py:

- Used by: run_pipeline.py is used by you, in the terminal; api.py by other programs, over the web
- Saves to: run_pipeline.py saves to a local folder (GCS with --upload); api.py always saves to GCS
- Reads results back: run_pipeline.py no; api.py yes
