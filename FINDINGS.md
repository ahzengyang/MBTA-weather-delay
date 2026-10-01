# Source 2 (Weather) — Significant Findings

Everything below was verified against the live Open-Meteo API between
2026-09-28 and 2026-09-29, and against the 1,250,496-row dataset produced by
`backfill.py`. Each finding changed either the code or the analysis plan.

---

## 1. Open-Meteo's `timezone` parameter is not DST-aware 🔴

**The bug.** Requesting `timezone=America/New_York` returns timestamps in local
time, but the API applies **one fixed UTC offset to the entire request** rather
than real per-hour DST rules. A request for 2025-11-02 — an EST date — comes
back with `utc_offset_seconds: -14400` (EDT, −4h).

**Proof.** The value Open-Meteo labels `2025-11-02T12:00` local is identical to
the value at `16:00Z`. True local noon that day is `17:00Z`.

```
'America/New_York' 2025-11-02T12:00  temp = 8.9
  GMT  2025-11-02T15:00  temp = 7.6
  GMT  2025-11-02T16:00  temp = 8.9   <-- MATCH  (= 11:00 EST, not 12:00)
  GMT  2025-11-02T17:00  temp = 9.7
```

**Why it is dangerous.** It fails silently. The API returns a clean 24 rows per
day with no duplicate and no gap, so `tz_localize(..., ambiguous="NaT")` never
triggers — the guard is dead code. Every row in the EST half of the year is
shifted one hour, which is precisely the snow-and-nor'easter season the research
question is about.

**Fix.** Always request `timezone=GMT`, parse as UTC, then `tz_convert` to local.
UTC has no ambiguous or nonexistent hours, so the conversion is exact.

**Verified in the delivered dataset:**

| Check | Result |
|---|---|
| `2024-01-15T17:00Z` | → `12:00-05:00` ✅ EST |
| `2024-07-15T16:00Z` | → `12:00-04:00` ✅ EDT |
| Fall-back 2024-11-03, 01:00 local | appears **twice**, from `05:00Z` and `06:00Z` ✅ |
| Spring-forward 2024-03-10, 02:00 local | **0 rows** ✅ (that hour does not exist) |

> **Team impact:** all joins must be on UTC. `observed_at_local` is for display
> only — it repeats an hour every November and skips one every March, so it is
> not a valid key. Source 1 should emit `arrival_at_utc`.

---

## 2. `precipitation` includes snow, so the rain flag counted snowstorms 🔴

`precipitation` is rain **plus snow water equivalent**. Keying `is_raining` on
`precipitation > 0` therefore labels every snowstorm as rain.

Across the full 52-station dataset:

| Flag | Hours | Share |
|---|---|---|
| `is_raining` (rain > 0) | 66,489 | 5.3% |
| `is_snowing` (snowfall > 0) | 17,307 | 1.4% |
| `is_precipitating` (precipitation > 0) | 66,603 | 5.3% |

The original flag would have reported all **66,603** hours as rain, folding in
17,307 hours of snow. Since snow plausibly delays trains far more than rain,
conflating them would have muddied the central result. Now split into three
separate flags.

---

## 3. Weather barely varies across the MBTA network ⭐

**This is the most consequential finding for the analysis.**

Across all 52 stations over 2024–2026:

| | Driest / least | Wettest / most | Spread |
|---|---|---|---|
| Rain | Ashmont 5.20% | Braintree 5.48% | **0.28 pp** |
| Snow | North Quincy 1.31% | Davis 1.45% | **0.14 pp** |

Metro Boston is effectively **one weather system**. Chasing spatial resolution
is close to pointless: the *where* carries almost no signal.

**What this means.** The study's power comes from the **temporal** dimension —
rainy hour vs. dry hour — crossed with **station type** (underground vs.
exposed). Not from geography. The design should be framed as:

> *Within the same hour and the same weather, do exposed stations show more
> delay than underground ones?*

---

## 4. Stations collapse into shared grid cells

Even on the historical-forecast ~2 km grid, **52 stations occupy only 18
distinct grid cells**:

| Cell size | Number of cells |
|---|---|
| 12 stations | 1 |
| 6 stations | 1 |
| 4 stations | 1 |
| 3 stations | 5 |
| 2 stations | 5 |
| 1 station | 5 |

The largest cell holds 12 downtown stations with byte-identical weather:
Aquarium, Bowdoin, Charles/MGH, Chinatown, Community College, Downtown
Crossing, Government Center, Haymarket, North Station, and more.

On the ERA5 `archive` endpoint (~25 km) it is far worse — Downtown Boston and
Boston College snap to the *identical* coordinate `42.355007, -71.12906`.

**Statistical consequence.** There are 52 stations but only **18 independent
weather observations**. Twelve downtown stations during one storm are one
observation of rain, not twelve. Standard errors should be clustered by
(cell × storm) or any significance claim will be overstated.

**The opportunity.** That 12-station downtown cell holds weather perfectly
constant while underground/surface status varies within it. That is close to a
**natural experiment** — a stronger design than comparing across cells, where
weather and station type both change at once. Combined with finding 3, this is
where the analysis should focus.

---

## 5. Endpoint choice: `historical_forecast` beats `archive` for 2024+

| | `archive` (ERA5) | `historical_forecast` |
|---|---|---|
| Coverage | 1940 → yesterday | 2022 → today |
| Grid | ~25 km | ~2 km |
| `visibility` | **accepted, returns all-nulls** | real values, 100% populated |

Because the project only needs 2024 onward, `archive`'s single advantage — deep
history — is irrelevant. Both endpoints are identical effort (one parameter),
so `historical_forecast` is chosen: it keeps stations spatially distinct and
supplies visibility.

**Trap worth knowing:** requesting `visibility` from `archive` does **not**
error. It returns `"visibility": "undefined"` units and a column of nulls. The
`_validate()` all-null check exists to catch exactly this class of failure.

---

## 6. The "~5-day archive lag" is outdated

The archive endpoint returned complete, non-null data **through yesterday**.
However, the most recent ~5 days are **ERA5T preliminary** values that ERA5
final later revises.

Consequence for scheduling: a daily job should re-fetch a **trailing window**
(~10 days), not just yesterday, so preliminary values get overwritten rather
than frozen. The SQLite primary key `(location, observed_at_utc)` with
`INSERT OR REPLACE` makes this a safe upsert — verified idempotent: two
identical runs leave 672 rows, not 1,344.

---

## 7. Open-Meteo cannot supply station coordinates

Open-Meteo is a weather API — it takes a lat/lon and returns weather. Its
geocoding endpoint is a place-name gazetteer with no transit awareness:

| Query | Top result |
|---|---|
| `Wonderland` | Wonderland, **Maine** |
| `Park Street` | Park Street, **England** |

Coordinates therefore come from the **MBTA v3 API** (`api-v3.mbta.com`), which
needs no key and is authoritative — the same agency that produces the delay
data in source 1, so station naming lines up. See `get_stations.py`.

---

## 8. Station count: 125 stops vs. the 50 in the problem statement

MBTA's API returns **125 distinct parent stations** for subway route types
(0 = light rail, 1 = heavy rail):

| Line | Stations |
|---|---|
| Red | 22 |
| Orange | 20 |
| Blue | 12 |
| **Red + Orange + Blue, deduped** | **52** |
| Green B / C / D / E | 23 / 20 / 25 / 25 |
| Mattapan | 8 |

Heavy rail alone is **52**, matching the problem statement's ~50. The other 73
are Green Line and Mattapan street-running trolley stops (Babcock Street, Amory
Street, Summit Avenue) — curbside platforms rather than stations, which also
will not appear in station-level ridership data.

**Decision:** back-fill the 52 heavy-rail stations. `stations_heavy_rail.csv`
holds the canonical `station_name` spellings; sources 1 and 3 must match them
exactly or joins will silently drop rows.

---

## 9. The whole date range fits in one request per station

2024-01-01 → 2026-09-28 is 24,048 hourly rows, returned in a **single** call
with zero nulls. So the backfill is 52 requests against a ~10,000/day limit —
no chunking, no batching, ~4 minutes end to end.

---

## 10. SQLite schema was frozen by whichever endpoint ran first 🔴

Caught by a two-station smoke test. `save_processed` built the table with
`CREATE TABLE IF NOT EXISTS` from whatever columns the first DataFrame had. An
`archive` run (no visibility) froze a schema that a later
`historical_forecast` run could not write into:

```
sqlite3.OperationalError: table weather_hourly has no column named visibility
```

**Fix.** Pin an explicit `TABLE_COLUMNS` list covering both endpoints, and
`reindex` each frame onto it so absent columns store `NULL`.

> **Team impact:** anyone writing a DataFrame to SQLite with
> `CREATE TABLE IF NOT EXISTS` has this same latent bug.

---

## Delivered dataset

| | |
|---|---|
| Rows | **1,250,496** (52 stations × 24,048 hours) |
| Range | 2024-01-01 → 2026-09-28, hourly |
| Endpoint | `historical_forecast` (~2 km grid, visibility) |
| Failures | 0 |
| Nulls | 0 in every column |
| Short stations | 0 — all 52 have exactly 24,048 rows |
| SQLite | 305 MB (gitignore — over GitHub's 100 MB limit) |
| Parquet | 13 MB ← share this |
| Raw gzipped JSON | 12 MB, 52 files (full audit trail) |

---

## Open blocker

`is_underground` is **empty**. The MBTA API has no such field — it is source 4's
(Wikipedia) deliverable. The weather data is complete, but **the study cannot
run until that column is filled for these 52 stations.** Given findings 3 and 4,
the 12 downtown stations sharing one grid cell are the highest-value ones to
classify first.
