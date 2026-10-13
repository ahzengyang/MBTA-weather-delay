This shows how many riders were at each station at hour,
to show how weather events affect ridership.

The data source

https://mbta-massdot.opendata.arcgis.com/datasets/001c177f07594e7c99f193dde32284c9

- 2 years of data
- Entries only, MBTA doesn't publish exits
- Public, no API key

API endpoint:

https://services1.arcgis.com/ceiitspzDAHrdGO1/arcgis/rest/services/GSE/FeatureServer/0/query

Files at a glance:

- stations.py: The 69 station IDs we keep
- gse_fetch.py: 1: download raw data
- gse_clean.py: 2: clean, then build hourly and daily totals
- gse_baseline.py: 3: normal ridership, and storm days compared to it
- run_pipeline.py: Runs steps 1–3 and saves the results
- gcs.py: Uploads to and reads from the GCS bucket
- api.py: FastAPI app that runs the pipeline and serves results
- storm_days.csv: 25 storm dates, from Alex
- GSE_event_vs_baseline.csv: storm day vs normal, per station
- GSE_hourly_all.csv: hourly entries per station, every day
- requirements.txt: Python libraries
- .env_template: Settings to copy into .env

How the pipeline works

```
storm_days.csv ──► gse_fetch ──► gse_clean ──► gse_baseline ──► gcs.py ──► GCS bucket
                   └──────────── run_pipeline.py runs all of these ─────────┘

api.py = the same pipeline, triggered over the web, plus endpoints to read results
```

Calls, in order

```
1. gse_fetch.available_range()      first and last date the data covers;
                                    storm dates outside that range are dropped
2. baseline window                  the last 365 days, minus storm dates
3. gse_fetch.load()                 one download for all days (~1.1M rows, 71 stations)
4. stations.STOP_IDS                keep 69 stations
5. gse_clean.clean()                drop bad and duplicate rows, fix timestamps,
                                    merge transfer stations
6. daily_totals ──► baseline()      normal ridership per station
7. daily_totals ──► compare()       each storm day vs normal
8. hourly_totals()                  hourly table, with an event_day flag
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
- `clean()` drops bad and duplicate rows, fixes timestamps, and merges
  transfer stations (e.g. Park St Red + Green) into one row.
- `hourly_totals()` gives one row per station per hour.
- `daily_totals()` gives one row per station per day.
- Each row is tagged `weekday` or `weekend`.

gse_baseline.py:
- `baseline()` gives each station's **normal** daily ridership: a 365-day
  average, with storm days excluded, split into weekday and weekend.
- `compare()` compares each storm day to normal for the same kind of day:
  `pct_vs_baseline = (storm entries / normal − 1) × 100`.
  For example, `-17.9` means 17.9% fewer riders.

run_pipeline.py:
- `run()` holds the steps in "Calls, in order" above.
- `save_raw()` uploads raw data to GCS, one CSV per month.
- Command line options: `--local GSE.csv` reads a downloaded file;
  `--upload` also sends the results to GCS.

gcs.py:
- `save_csv()` and `read_csv()` upload and download CSVs.
- Settings come from `.env`. With no key file, it uses your `gcloud` login.

api.py:
- Has no processing logic of its own. It calls the same `run()`.
- `POST /ridership/refresh` runs the pipeline and uploads the results.
- `GET /ridership/raw`, `/hourly-all` and `/event-vs-baseline` read
  results from the bucket.

run_pipeline.py vs api.py:

- Used by: run_pipeline.py is used by you, in the terminal; api.py by other programs, over the web
- Saves to: run_pipeline.py saves to a local folder (GCS with --upload); api.py always saves to GCS
- Reads results back: run_pipeline.py no; api.py yes
