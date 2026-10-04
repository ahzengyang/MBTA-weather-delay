#!/usr/bin/env python3
"""
source1_mbta_lamp_delay.py
==========================

MBTA LAMP subway performance data -> per-station, per-local-hour "excess travel
time" panel, for joining to hourly weather by (station, hour).

SOURCE
------
MBTA LAMP (Lightweight Application for Measuring Performance), "Subway
Performance Data" public export. One Parquet file per service date:

    https://performancedata.mbta.com/lamp/subway-on-time-performance-v1/
        YYYY-MM-DD-subway-on-time-performance-v1.parquet

Index of published dates: .../subway-on-time-performance-v1/index.csv
Data dictionary:          https://github.com/mbta/lamp/blob/main/Data_Dictionary.md
No API key. One row per trip_id-stop_id pair per service_date.

FIELDS USED
-----------
travel_time_seconds      actual seconds travelling TO this stop FROM the
                         previous stop on the trip              (LAMP calculated)
scheduled_travel_time    planned seconds for that same hop      (LAMP calculated)
headway_trunk_seconds    seconds between consecutive vehicles departing
                         parent_station on the trunk line       (LAMP calculated)
scheduled_headway_trunk  planned version of the above           (LAMP calculated)
parent_station           station NAME (the GTFS stop_name of the parent
                         station), not an ID. There is no stop_name column.
trip_id, stop_sequence,  trip identity and within-trip ordering
service_date             (service_date is an int64 YYYYMMDD)
stop_timestamp           POSIX epoch seconds (UTC). Used ONLY to bucket rows
                         into America/New_York clock hours for the weather join.

Delay is measured with LAMP's own like-for-like differences:

    excess_travel_s  = travel_time_seconds   - scheduled_travel_time
    excess_headway_s = headway_trunk_seconds - scheduled_headway_trunk

Both sides of each difference come from the same publisher in the same units,
so no clock-basis or timezone error can leak in. The absolute-arrival approach
(stop_timestamp - scheduled_arrival_time) is deliberately NOT used: those two
fields are on different clocks, and the basis of scheduled_arrival_time is not
stated in the dictionary.

ATTRIBUTION CAVEAT: SEGMENTS, NOT STATIONS
------------------------------------------
excess_travel_s belongs to the track SEGMENT between the previous stop and this
stop, not to a single station. This export has no previous_stop_id, so the
segment origin is derived by sorting each trip on stop_sequence and shifting
parent_station down one row (prev_parent_station). Consequences:

* The origin is the previous RECORDED stop. If an intermediate arrival was not
  recorded, the derived segment may span more than one hop. Such rows are
  flagged (seq_gap_suspect) using the trip's typical stop_sequence step.
* The first recorded stop of each trip legitimately has no origin.
* Rows sharing a stop_sequence within one trip make the shift ambiguous; they
  (and the row after them) are flagged origin_ambiguous.

In station_hour_panel() a segment's excess time is keyed to its DESTINATION
station, which is where the arrival (and stop_timestamp) happened.

excess_headway_s is station-level by definition (departures from
parent_station), so it needs no segment handling. It is included as a
secondary, cleanly attributable measure.

No row is dropped silently. add_excess_columns() never removes rows; it writes
a drop_reason (and headway_drop_reason) for every excluded row, and
run_checks() reports each reason's count.

OUTPUT: GOOGLE CLOUD STORAGE
----------------------------
Nothing is written locally. The panel CSV is uploaded straight from memory:

    gs://BUCKET/PREFIX/processed/mbta_station_hour_panel_START_to_END.csv
    gs://BUCKET/PREFIX/raw/YYYY-MM-DD-subway-on-time-performance-v1.parquet  (--raw)

PREFIX defaults to "mbta_lamp". Re-running the same dates overwrites the
objects. Credentials come from user_definition.py in the same folder, which
must define project_id, bucket_name and service_account_file_path.

Requires: requests, pandas, pyarrow, google-cloud-storage, python-dotenv.
Usage:    python source1_mbta_lamp_delay.py [--range START END | --date YYYY-MM-DD]
                                            [--raw] [--prefix PREFIX] [--no-checks]

          --range 2025-03-01 2025-03-31   fetches every service date in the
          inclusive range and uploads ONE panel CSV covering all of them.
          Dates with no published file are skipped and listed at the end.
          With neither --range nor --date, the latest complete date is used.
"""

from __future__ import annotations

import argparse
import gzip
import io
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from google.api_core.exceptions import GoogleAPIError
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY

from user_definition import bucket_name, project_id, service_account_file_path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_URL = "https://performancedata.mbta.com/lamp/subway-on-time-performance-v1"
INDEX_URL = f"{BASE_URL}/index.csv"
FILE_TEMPLATE = "{d}-subway-on-time-performance-v1.parquet"
LOCAL_TZ = "America/New_York"
HTTP_TIMEOUT_S = 180
USER_AGENT = "mbta-weather-delay-class-project/1.0 (python-requests)"

# GCS upload settings
DEFAULT_PREFIX = "mbta_lamp"
UPLOAD_TIMEOUT_S = 300
UPLOAD_RETRY = DEFAULT_RETRY.with_deadline(600)   # retry 429/5xx/connection errors up to 10 min
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"

TRIP_KEY = ["service_date", "trip_id"]

REQUIRED_COLUMNS = (
    "service_date",
    "trip_id",
    "stop_id",
    "stop_sequence",
    "parent_station",
    "stop_timestamp",
    "travel_time_seconds",
    "scheduled_travel_time",
    "headway_trunk_seconds",
    "scheduled_headway_trunk",
)

# Exclusion rule: an excess this large is a data error (mis-matched trip,
# vehicle held out of service, merged segments), not a weather effect.
# Deliberately looser than the ±30 min plausibility test in run_checks(), which
# is evaluated BEFORE this cap so the cap cannot hide a failure.
IMPLAUSIBLE_ABS_EXCESS_S = 60 * 60

# Drop reasons, applied as a waterfall in this order (each row gets at most one).
DROP_NULL = "null_component"
DROP_UNMATCHED = "unmatched_trip (scheduled == 0)"
DROP_IMPLAUSIBLE = "implausible_magnitude"
DROP_NO_TS = "no_stop_timestamp (missing/invalid, cannot assign hour)"
# stop_timestamp outside this window (2014-05 .. 2039-09) cannot be POSIX seconds
PLAUSIBLE_TS_RANGE = (1.4e9, 2.2e9)
DROP_NO_STATION = "no_destination_station"
DROP_ORDER = [DROP_NULL, DROP_UNMATCHED, DROP_IMPLAUSIBLE, DROP_NO_TS, DROP_NO_STATION]

# Verification thresholds (tune here, not inside run_checks)
MIN_DEST_NONNULL_SHARE = 0.99
MAX_UNEXPLAINED_NULL_ORIGIN_SHARE = 0.01
MIN_STATIONS, MAX_STATIONS = 80, 180     # MBTA subway incl. Green surface stops ~ 120-150
MEDIAN_FAIL_MIN = 2.0
TAIL_LIMIT_MIN = 30.0
TAIL_FAIL_SHARE = 0.05
MIN_USABLE_SHARE = 0.50
MAX_UNMATCHED_SHARE = 0.15
MIN_SAME_DATE_SHARE = 0.90
MAX_UNEXPLAINED_DATE_SHARE = 0.01
AFTER_MIDNIGHT_LAST_HOUR = 4             # local hour < 4 on service_date+1 is late service
MIN_MEDIAN_OBS_PER_CELL = 10
SPARSE_CELL_OBS = 5


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _as_date(value: date | datetime | str) -> date:
    """Accept date, datetime, 'YYYY-MM-DD' or 'YYYYMMDD'."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10].replace("-", "")
    return datetime.strptime(text, "%Y%m%d").date()


def _to_float(s: pd.Series) -> pd.Series:
    """Numeric coercion that survives pandas nullable dtypes (Int64/Float64 with NA)."""
    num = pd.to_numeric(s, errors="coerce")
    return pd.Series(num.to_numpy(dtype="float64", na_value=np.nan), index=s.index, name=s.name)


def _service_date_as_date(s: pd.Series) -> pd.Series:
    """service_date is int64 YYYYMMDD per the dictionary; also tolerate 'YYYY-MM-DD'."""
    text = s.astype("string").str.strip().str.slice(0, 10).str.replace("-", "", regex=False)
    return pd.to_datetime(text, format="%Y%m%d", errors="coerce").dt.date


def _pct(n: float, d: float) -> float:
    return 100.0 * n / d if d else float("nan")


def _header(i: int, title: str) -> None:
    print(f"\n[{i}] {title}")
    print("-" * 78)


def _verdict(ok: bool, msg: str) -> None:
    print(f"  >>> {'PASS' if ok else 'FAIL'}: {msg}")


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #
def _parse_index_text(text: str) -> list[date]:
    """Extract service dates from index.csv text without assuming its exact layout."""
    found: set[date] = set()

    # 1) Any column whose header mentions "date".
    try:
        idx = pd.read_csv(io.StringIO(text), dtype=str)
        for col in idx.columns:
            if "date" in str(col).lower():
                parsed = _service_date_as_date(idx[col])
                found.update(d for d in parsed.dropna())
    except Exception:  # malformed CSV -> fall through to the regex passes
        pass

    # 2) Dates embedded in file names / URLs.
    for m in re.finditer(r"(\d{4}-\d{2}-\d{2})-subway-on-time-performance", text):
        found.add(_as_date(m.group(1)))

    # 3) Last resort: any ISO date in the text.
    if not found:
        for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", text):
            try:
                found.add(_as_date(m.group(1)))
            except ValueError:
                continue

    today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    return sorted(d for d in found if date(2019, 1, 1) <= d <= today + timedelta(days=1))


def list_service_dates(session: requests.Session | None = None) -> list[date]:
    """Return the published service dates listed in LAMP's index.csv, ascending."""
    sess = session or _session()
    resp = sess.get(INDEX_URL, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    content = resp.content
    if content[:2] == b"\x1f\x8b":  # stored gzipped without Content-Encoding
        content = gzip.decompress(content)
    text = content.decode("utf-8-sig", errors="replace")
    dates = _parse_index_text(text)
    if not dates:
        raise RuntimeError(
            f"Could not parse any service dates from {INDEX_URL}. "
            f"First 300 characters: {text[:300]!r}"
        )
    return dates


def fetch_service_date(
    service_date: date | datetime | str,
    session: requests.Session | None = None,
    raw_sink: Callable[[str, bytes], None] | None = None,
) -> pd.DataFrame:
    """Download one service date's Parquet file and return it unmodified.
    If raw_sink is given, it is called with (file_name, original_bytes),
    e.g. to upload the raw file to GCS."""
    d = _as_date(service_date)
    fname = FILE_TEMPLATE.format(d=d.isoformat())
    url = f"{BASE_URL}/{fname}"
    sess = session or _session()
    resp = sess.get(url, timeout=HTTP_TIMEOUT_S)
    if resp.status_code == 404:
        raise FileNotFoundError(f"No LAMP file published for {d}: {url}")
    resp.raise_for_status()
    if raw_sink is not None:
        raw_sink(fname, resp.content)
    raw = pd.read_parquet(io.BytesIO(resp.content), engine="pyarrow")
    raw.attrs["source_url"] = url
    raw.attrs["requested_date"] = d.isoformat()
    return raw


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #
def add_excess_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Add segment endpoints, excess times, exclusion reasons and local hour.
    Never drops rows; returns a copy sorted by (service_date, trip_id, stop_sequence).

    Added columns
      prev_parent_station, prev_stop_id   segment origin (previous recorded stop)
      is_trip_first_row                   True where an origin is legitimately absent
      origin_ambiguous                    duplicate stop_sequence makes the shift unsafe
      seq_gap_suspect                     stop_sequence jump > 1.5x trip's median step
      excess_travel_s / excess_travel_min
      excess_headway_s / excess_headway_min
      drop_reason, usable                 travel measure (headline)
      headway_drop_reason, usable_headway headway measure (secondary)
      hour_utc, hour_local, local_date    from stop_timestamp only
      service_date_parsed
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise KeyError(f"LAMP frame is missing expected columns: {missing}")

    df = raw.copy()
    for col in ("stop_sequence", "stop_timestamp", "travel_time_seconds",
                "scheduled_travel_time", "headway_trunk_seconds", "scheduled_headway_trunk"):
        df[col] = _to_float(df[col])

    # --- Invalid timestamps -> NaN ------------------------------------------------
    # Some files contain stop_timestamp values far outside POSIX-seconds range
    # (corrupt/sentinel values), which make pd.to_datetime overflow. Treat them as
    # missing: they then fall under DROP_NO_TS, so they are counted, not hidden.
    ts = df["stop_timestamp"]
    bad_ts = ts.notna() & ~ts.between(*PLAUSIBLE_TS_RANGE)
    n_bad = int(bad_ts.sum())
    if n_bad:
        print(f"  WARNING: {n_bad:,} stop_timestamp value(s) outside the plausible POSIX-seconds "
              f"range set to missing (examples: {ts[bad_ts].head(3).tolist()})")
        df.loc[bad_ts, "stop_timestamp"] = np.nan
    df.attrs["n_invalid_timestamps"] = n_bad

    # --- Segment origin: sort within trip on stop_sequence, shift by one ---------
    df = df.sort_values(
        TRIP_KEY + ["stop_sequence", "stop_timestamp"],
        kind="mergesort", na_position="last",
    ).reset_index(drop=True)
    g = df.groupby(TRIP_KEY, sort=False, dropna=False)
    df["prev_parent_station"] = g["parent_station"].shift(1)
    df["prev_stop_id"] = g["stop_id"].shift(1)
    df["is_trip_first_row"] = g.cumcount().eq(0)

    dup_seq = df.duplicated(TRIP_KEY + ["stop_sequence"], keep=False)
    prev_dup = (
        dup_seq.groupby([df[k] for k in TRIP_KEY], sort=False, dropna=False)
        .shift(1).astype("boolean").fillna(False).astype(bool)
    )
    df["origin_ambiguous"] = (dup_seq | prev_dup) & ~df["is_trip_first_row"]

    step = df["stop_sequence"] - g["stop_sequence"].shift(1)
    typical_step = step.groupby([df[k] for k in TRIP_KEY], sort=False, dropna=False).transform("median")
    df["seq_gap_suspect"] = (typical_step > 0) & (step > 1.5 * typical_step)

    # --- Excess times ------------------------------------------------------------
    tt, stt = df["travel_time_seconds"], df["scheduled_travel_time"]
    hw, shw = df["headway_trunk_seconds"], df["scheduled_headway_trunk"]
    df["excess_travel_s"] = tt - stt
    df["excess_travel_min"] = df["excess_travel_s"] / 60.0
    df["excess_headway_s"] = hw - shw
    df["excess_headway_min"] = df["excess_headway_s"] / 60.0

    # --- Local hour from stop_timestamp (POSIX seconds). Floor in UTC, then
    # convert: EST/EDT offsets are whole hours, and this avoids ambiguous-time
    # errors on the November fall-back night.
    utc = pd.to_datetime(df["stop_timestamp"], unit="s", utc=True, errors="coerce")
    df["hour_utc"] = utc.dt.floor("h")
    df["hour_local"] = df["hour_utc"].dt.tz_convert(LOCAL_TZ)
    df["local_date"] = utc.dt.tz_convert(LOCAL_TZ).dt.date
    df["service_date_parsed"] = _service_date_as_date(df["service_date"])

    no_ts = df["hour_utc"].isna()
    no_station = df["parent_station"].isna()

    # --- Travel exclusion waterfall -------------------------------------------------
    t_null = tt.isna() | stt.isna()
    t_unmatched = ~t_null & stt.eq(0)
    t_implaus = ~t_null & ~t_unmatched & (
        tt.le(0) | stt.lt(0) | df["excess_travel_s"].abs().gt(IMPLAUSIBLE_ABS_EXCESS_S)
    )
    t_prior = t_null | t_unmatched | t_implaus
    t_no_ts = ~t_prior & no_ts
    t_no_st = ~t_prior & ~no_ts & no_station
    df["drop_reason"] = pd.Series(
        np.select([t_null, t_unmatched, t_implaus, t_no_ts, t_no_st], DROP_ORDER, default=None),
        index=df.index, dtype="object",
    )
    df["usable"] = df["drop_reason"].isna()

    # --- Headway exclusion waterfall (secondary measure) --------------------------
    h_null = hw.isna() | shw.isna()
    h_unmatched = ~h_null & shw.le(0)
    h_implaus = ~h_null & ~h_unmatched & (
        hw.le(0) | df["excess_headway_s"].abs().gt(IMPLAUSIBLE_ABS_EXCESS_S)
    )
    h_prior = h_null | h_unmatched | h_implaus
    h_no_ts = ~h_prior & no_ts
    h_no_st = ~h_prior & ~no_ts & no_station
    df["headway_drop_reason"] = pd.Series(
        np.select([h_null, h_unmatched, h_implaus, h_no_ts, h_no_st], DROP_ORDER, default=None),
        index=df.index, dtype="object",
    )
    df["usable_headway"] = df["headway_drop_reason"].isna()
    return df


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _p90(x: pd.Series) -> float:
    return float(x.quantile(0.90)) if len(x) else float("nan")


def station_hour_panel(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate to (parent_station, hour_local). Travel stats use `usable` rows,
    keyed to the segment's DESTINATION station.

    Columns: parent_station, hour_local, hour_utc, n_arrivals (all rows with a
    timestamp at that station-hour), n_travel_obs, excess_travel_min_{median,
    mean,p90,sum}, n_headway_obs, excess_headway_min_median.
    """
    keys = ["parent_station", "hour_local"]
    base = df[df["usable"]]

    arrivals = (df[df["hour_local"].notna() & df["parent_station"].notna()]
                .groupby(keys, observed=True).size().rename("n_arrivals"))

    travel = base.groupby(keys, observed=True)["excess_travel_min"].agg(
        n_travel_obs="count",
        excess_travel_min_median="median",
        excess_travel_min_mean="mean",
        excess_travel_min_p90=_p90,
        excess_travel_min_sum="sum",
    )

    hw = df[df["usable_headway"]].groupby(keys, observed=True)["excess_headway_min"].agg(
        n_headway_obs="count", excess_headway_min_median="median",
    )

    panel = pd.concat([arrivals, travel, hw], axis=1)

    count_cols = [c for c in panel.columns if c.startswith("n_")]
    panel[count_cols] = panel[count_cols].fillna(0).astype("int64")
    panel = panel.reset_index()
    panel.insert(2, "hour_utc", panel["hour_local"].dt.tz_convert("UTC"))
    return panel.sort_values(keys).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def run_checks(df: pd.DataFrame) -> dict[str, bool]:
    """Print the verification report and return {check_name: passed}."""
    results: dict[str, bool] = {}
    n = len(df)
    src = df.attrs.get("source_url", "(unknown source)")
    print("=" * 78)
    print(f"LAMP VERIFICATION REPORT  -  {n:,} rows  -  {src}")
    print("=" * 78)

    # 1. STATION ATTRIBUTION ----------------------------------------------------
    _header(1, "STATION ATTRIBUTION")
    dest_ok = df["parent_station"].notna()
    orig_ok = df["prev_parent_station"].notna()
    first = df["is_trip_first_row"]
    n_trips = int(first.sum())
    null_orig_first = int((~orig_ok & first).sum())
    null_orig_other = int((~orig_ok & ~first).sum())
    tt_no_origin = int((df["travel_time_seconds"].notna() & ~orig_ok).sum())
    self_seg = int((orig_ok & df["prev_parent_station"].eq(df["parent_station"])).sum())
    n_stations = int(df["parent_station"].dropna().nunique())
    print(f"  Destination station (parent_station) non-null: {_pct(dest_ok.sum(), n):6.2f}%")
    print(f"  Segment origin (prev_parent_station) non-null: {_pct(orig_ok.sum(), n):6.2f}%")
    print(f"    Null origins at a trip's first recorded stop: {null_orig_first:,} "
          f"(legitimate: {n_trips:,} trips, so the first stop has no predecessor)")
    print(f"    Null origins elsewhere (previous row had no station): {null_orig_other:,} "
          f"({_pct(null_orig_other, n):.2f}%)")
    print(f"  Rows where LAMP reports travel time but no origin row exists: {tt_no_origin:,} "
          f"(LAMP saw a previous stop missing from this file)")
    print(f"  Origin ambiguous (duplicate stop_sequence in trip): {int(df['origin_ambiguous'].sum()):,}")
    print(f"  Possible skipped stop (stop_sequence jump): {int(df['seq_gap_suspect'].sum()):,} "
          f"({_pct(df['seq_gap_suspect'].sum(), n):.2f}%)")
    print(f"  Self-segments (origin == destination station): {self_seg:,}")
    print(f"  Distinct destination stations: {n_stations} "
          f"(plausible MBTA subway range {MIN_STATIONS}-{MAX_STATIONS}, incl. Green Line surface stops)")
    print(f"  Sample names: {sorted(df['parent_station'].dropna().astype(str).unique())[:6]}")
    ok1 = (dest_ok.mean() >= MIN_DEST_NONNULL_SHARE
           and null_orig_other / max(n, 1) <= MAX_UNEXPLAINED_NULL_ORIGIN_SHARE
           and MIN_STATIONS <= n_stations <= MAX_STATIONS)
    _verdict(ok1, "every row names its destination, origins are missing only where expected, "
             "and the station count is plausible" if ok1 else
             "attribution is incomplete or the station count is implausible; see figures above")
    results["station_attribution"] = ok1

    # 2. DELAY PLAUSIBILITY ----------------------------------------------------
    _header(2, "DELAY PLAUSIBILITY (excess_travel_min, before the magnitude cap)")
    tt, stt = df["travel_time_seconds"], df["scheduled_travel_time"]
    matched = tt.notna() & stt.notna() & stt.ne(0)
    x = df.loc[matched, "excess_travel_min"]
    if x.empty:
        _verdict(False, "no rows with both travel components and a matched schedule")
        results["delay_plausibility"] = False
    else:
        q = x.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        tail = float((x.abs() > TAIL_LIMIT_MIN).mean())
        print(f"  n = {len(x):,} matched rows with both components")
        print(f"  median {q[0.5]:+.2f}   IQR [{q[0.25]:+.2f}, {q[0.75]:+.2f}] (width {q[0.75] - q[0.25]:.2f})")
        print(f"  p5 {q[0.05]:+.2f}   p95 {q[0.95]:+.2f}   min {x.min():+.2f}   max {x.max():+.2f}")
        print(f"  share beyond ±{TAIL_LIMIT_MIN:.0f} min: {100 * tail:.2f}%")
        xu = df.loc[df["usable"], "excess_travel_min"]
        if len(xu):
            print(f"  (after exclusions: n = {len(xu):,}, median {xu.median():+.2f}, "
                  f"p5 {xu.quantile(0.05):+.2f}, p95 {xu.quantile(0.95):+.2f})")
        bad_median = abs(q[0.5]) > MEDIAN_FAIL_MIN
        bad_tail = tail > TAIL_FAIL_SHARE
        if bad_median:
            print("  IMPLICATION: a systematic offset means actual and scheduled travel times are "
                  "not describing the same hop (e.g. travel_time spans merged segments, or the "
                  "schedule match is wrong), so the measure is biased before weather enters.")
        if bad_tail:
            print("  IMPLICATION: heavy tails point to mis-matched trips, unrecorded intermediate "
                  "stops merging segments, or disruptions/shuttles. Hour-level means will be "
                  "dominated by these; prefer medians and inspect the tail rows.")
        ok3 = not bad_median and not bad_tail
        _verdict(ok3, "centred near zero with most mass within a few minutes" if ok3 else
                 f"|median| {'>' if bad_median else '<='} {MEDIAN_FAIL_MIN} min, "
                 f"tail {100 * tail:.1f}% {'>' if bad_tail else '<='} {100 * TAIL_FAIL_SHARE:.0f}%")
        results["delay_plausibility"] = ok3

    # 3. DATA LOSS -----------------------------------------------------------
    _header(3, "DATA LOSS (waterfall: each row counted under its first failing reason)")
    for label, col in (("Excess travel (headline)", "drop_reason"),
                       ("Excess headway (secondary)", "headway_drop_reason")):
        print(f"  {label}:")
        vc = df[col].value_counts()
        for reason in DROP_ORDER:
            k = int(vc.get(reason, 0))
            print(f"    {reason:<42} {k:>9,}  {_pct(k, n):6.2f}%")
            if col == "drop_reason" and reason == DROP_NULL and k:
                kf = int((df[col].eq(DROP_NULL) & first).sum())
                print(f"      of which trip first stop (expected, no prior hop): {kf:,}; other: {k - kf:,}")
        kept = int(df[col].isna().sum())
        print(f"    {'KEPT':<42} {kept:>9,}  {_pct(kept, n):6.2f}%")
    usable_share = float(df["usable"].mean()) if n else 0.0
    unmatched_share = float(df["drop_reason"].eq(DROP_UNMATCHED).mean()) if n else 0.0
    ok4 = usable_share >= MIN_USABLE_SHARE and unmatched_share <= MAX_UNMATCHED_SHARE
    _verdict(ok4, f"{100 * usable_share:.1f}% of rows usable; unmatched trips "
             f"{100 * unmatched_share:.1f}% (limits: >= {100 * MIN_USABLE_SHARE:.0f}% usable, "
             f"<= {100 * MAX_UNMATCHED_SHARE:.0f}% unmatched)")
    results["data_loss"] = ok4

    # 4. TIMESTAMP SANITY ------------------------------------------------------
    _header(4, "TIMESTAMP SANITY (stop_timestamp as POSIX seconds)")
    has_ts = df["local_date"].notna() & df["service_date_parsed"].notna()
    m = int(has_ts.sum())
    if m == 0:
        _verdict(False, "no rows with both stop_timestamp and service_date")
        results["timestamp_sanity"] = False
    else:
        ts_med = float(df["stop_timestamp"].median())
        mag_ok = 1.4e9 < ts_med < 2.2e9
        ld = pd.to_datetime(df.loc[has_ts, "local_date"])
        sd = pd.to_datetime(df.loc[has_ts, "service_date_parsed"])
        hr = df.loc[has_ts, "hour_local"].dt.hour
        same = ld.eq(sd)
        late = ld.eq(sd + pd.Timedelta(days=1)) & hr.lt(AFTER_MIDNIGHT_LAST_HOUR)
        other = ~same & ~late
        print(f"  Median stop_timestamp {ts_med:,.0f} -> {pd.to_datetime(ts_med, unit='s', utc=True)} "
              f"({'seconds-scale' if mag_ok else 'NOT seconds-scale'})")
        print(f"  Rows with a timestamp: {m:,} ({_pct(m, n):.2f}% of all rows)")
        print(f"  Local date == service_date:          {_pct(same.sum(), m):6.2f}%")
        print(f"  Next day before {AFTER_MIDNIGHT_LAST_HOUR:02d}:00 (late service): {_pct(late.sum(), m):6.2f}%")
        print(f"  Unexplained mismatches:              {_pct(other.sum(), m):6.2f}%")
        ok5 = mag_ok and same.mean() >= MIN_SAME_DATE_SHARE and other.mean() <= MAX_UNEXPLAINED_DATE_SHARE
        _verdict(ok5, "stop_timestamp behaves as POSIX seconds; hour bucketing is safe" if ok5 else
                 "stop_timestamp does not line up with service_date; do not trust hour_local")
        results["timestamp_sanity"] = ok5

    # 5. JOIN DENSITY --------------------------------------------------------
    _header(5, "JOIN DENSITY (station x hour cells)")
    p = station_hour_panel(df)
    cells = p[p["n_travel_obs"] > 0]
    ok6 = False
    print(f"  cells with >=1 arrival: {len(p):,}   with >=1 travel obs: {len(cells):,} "
          f"({p['parent_station'].nunique()} stations x {p['hour_local'].nunique()} hours)")
    if len(cells):
        obs = cells["n_travel_obs"]
        print(f"  travel obs per cell: median {obs.median():.0f}, min {obs.min()}, "
              f"p10 {obs.quantile(0.10):.0f}; cells with < {SPARSE_CELL_OBS}: "
              f"{int((obs < SPARSE_CELL_OBS).sum()):,} ({_pct((obs < SPARSE_CELL_OBS).sum(), len(obs)):.1f}%)")
        print(f"  arrivals per cell (all rows): median {p['n_arrivals'].median():.0f}")
        ok6 = obs.median() >= MIN_MEDIAN_OBS_PER_CELL
    _verdict(ok6, f"median travel observations per cell {'>=' if ok6 else '<'} {MIN_MEDIAN_OBS_PER_CELL}; "
             + ("hourly medians are reasonably stable" if ok6 else
                "hourly statistics will be noisy; consider pooling hours or dates")
             + f". Consider dropping cells with < {SPARSE_CELL_OBS} obs (typically late night).")
    results["join_density"] = ok6

    print("\n" + "=" * 78)
    print("SUMMARY: " + "  ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items()))
    print("=" * 78)
    return results


# --------------------------------------------------------------------------- #
# Google Cloud Storage upload
# --------------------------------------------------------------------------- #
def check_gcp_config() -> None:
    """Fail early, before any download, if user_definition.py didn't load the env file."""
    missing = [name for name, val in (("GCP_PROJECT_ID", project_id),
                                      ("GCP_BUCKET_NAME", bucket_name),
                                      ("GCP_SERVICE_ACCOUNT_KEY", service_account_file_path))
               if not val]
    if missing:
        raise RuntimeError(f"Missing values from the .env file: {', '.join(missing)}. "
                           "Check the load_dotenv() path in user_definition.py.")
    if not Path(service_account_file_path).is_file():
        raise FileNotFoundError(f"Service account key not found: {service_account_file_path}")


def get_bucket() -> storage.Bucket:
    client = storage.Client.from_service_account_json(service_account_file_path, project=project_id)
    # client.bucket() makes no API call, so this works even if the service account
    # only has object-level permissions (e.g. Storage Object Admin).
    return client.bucket(bucket_name)


def upload_bytes(bucket: storage.Bucket, blob_name: str, data: bytes | str, content_type: str) -> None:
    """Upload in-memory data, overwriting any existing object. Transient errors are retried."""
    size_mb = len(data) / 1e6
    print(f"  Uploading {size_mb:.1f} MB -> gs://{bucket.name}/{blob_name}")
    bucket.blob(blob_name).upload_from_string(
        data, content_type=content_type, timeout=UPLOAD_TIMEOUT_S, retry=UPLOAD_RETRY,
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _candidate_dates(dates: list[date], min_age_days: int) -> list[date]:
    """Most recent dates first, skipping ones too new to be complete (today's file grows live)."""
    cutoff = datetime.now(ZoneInfo(LOCAL_TZ)).date() - timedelta(days=min_age_days)
    return sorted((d for d in dates if d <= cutoff), reverse=True)


# Only these derived columns are needed by station_hour_panel(). Keeping just
# these per day keeps memory small when a range spans weeks or months.
PANEL_INPUT_COLUMNS = [
    "parent_station", "hour_local", "hour_utc",
    "usable", "excess_travel_min",
    "usable_headway", "excess_headway_min",
]


def panel_label(start: date, end: date) -> str:
    """File-name label: 'YYYY-MM-DD' for one day, 'START_to_END' for a range."""
    return start.isoformat() if start == end else f"{start.isoformat()}_to_{end.isoformat()}"


def process_day(d: date, sess: requests.Session, checks: bool,
                raw_sink: Callable[[str, bytes], None] | None = None,
                ) -> tuple[pd.DataFrame, list[str]]:
    """
    Fetch and derive one service date. Returns (slim frame with
    PANEL_INPUT_COLUMNS, names of failed checks). Raises FileNotFoundError /
    requests.RequestException if the file can't be fetched, and any other
    exception if it can't be processed.
    """
    raw = fetch_service_date(d, sess, raw_sink=raw_sink)
    print(f"  Loaded {len(raw):,} rows x {raw.shape[1]} columns")
    df = add_excess_columns(raw)
    del raw
    failed = [k for k, ok in run_checks(df).items() if not ok] if checks else []
    return df[PANEL_INPUT_COLUMNS].copy(), failed


def collect(days: list[date], sess: requests.Session, checks: bool,
            raw_sink: Callable[[str, bytes], None] | None = None,
            stop_after: int | None = None):
    """
    Fetch and derive each date in `days`. Returns (combined_slim_df or None,
    loaded_dates, skipped_dates, {date: failed_check_names}).
    stop_after=1 is used to take the first date that loads (default mode).
    """
    slim: list[pd.DataFrame] = []
    loaded: list[date] = []
    skipped: list[date] = []
    failed_checks: dict[date, list[str]] = {}

    for i, d in enumerate(days, 1):
        print(f"\n[{i}/{len(days)}] Fetching {d} ...")
        try:
            day_slim, failed = process_day(d, sess, checks, raw_sink)
        except (FileNotFoundError, requests.RequestException) as exc:
            print(f"  skipped: {exc}")
            skipped.append(d)
            continue
        except Exception as exc:  # one malformed file should not sink a long range
            print(f"  skipped: could not process {d}: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped.append(d)
            continue
        if failed:
            failed_checks[d] = failed
        slim.append(day_slim)
        loaded.append(d)
        if stop_after and len(loaded) >= stop_after:
            break

    combined = pd.concat(slim, ignore_index=True) if slim else None
    return combined, loaded, skipped, failed_checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--range", nargs=2, metavar=("START", "END"),
                       help="inclusive service-date range, e.g. --range 2025-03-01 2025-03-31; "
                            "uploads one combined CSV")
    which.add_argument("--date", help="single service date YYYY-MM-DD "
                                      "(default: latest complete date in index.csv)")
    ap.add_argument("--raw", action="store_true",
                    help="also upload each day's original Parquet file to PREFIX/raw/")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help=f"folder inside the bucket (default {DEFAULT_PREFIX})")
    ap.add_argument("--no-checks", action="store_true",
                    help="skip the verification report (useful for long ranges)")
    ap.add_argument("--min-age-days", type=int, default=2,
                    help="treat dates newer than this many days as possibly incomplete (default 2)")
    args = ap.parse_args(argv)
    prefix = args.prefix.strip("/")

    # --- GCP setup first, so a bad config fails before any downloading ------------
    try:
        check_gcp_config()
        bucket = get_bucket()
    except (RuntimeError, FileNotFoundError, ValueError, GoogleAPIError) as exc:
        print(f"GCP setup failed: {exc}", file=sys.stderr)
        return 2

    raw_failures: list[str] = []

    def raw_sink(fname: str, data: bytes) -> None:
        try:
            upload_bytes(bucket, f"{prefix}/raw/{fname}", data, PARQUET_CONTENT_TYPE)
        except GoogleAPIError as exc:
            print(f"  raw upload FAILED for {fname}: {exc}", file=sys.stderr)
            raw_failures.append(fname)

    # --- Which dates -----------------------------------------------------------------
    sess = _session()
    cutoff = datetime.now(ZoneInfo(LOCAL_TZ)).date() - timedelta(days=args.min_age_days)
    stop_after = None
    try:
        if args.range:
            start, end = (_as_date(x) for x in args.range)
        elif args.date:
            start = end = _as_date(args.date)
    except ValueError:
        ap.error("dates must be YYYY-MM-DD")

    if args.range or args.date:
        if end < start:
            ap.error(f"--range END ({end}) is before START ({start})")
        if end > cutoff:
            print(f"WARNING: dates after {cutoff} may be incomplete (files for recent days grow live).")
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        published = list_service_dates(sess)
        print(f"index.csv lists {len(published):,} dates ({published[0]} .. {published[-1]})")
        days = _candidate_dates(published, args.min_age_days)[:5]
        if not days:
            print("No published date is old enough; rerun with --min-age-days 0.", file=sys.stderr)
            return 2
        stop_after = 1  # take the newest date that actually loads

    # --- Fetch, derive, aggregate ------------------------------------------------------
    combined, loaded, skipped, failed_checks = collect(
        days, sess, checks=not args.no_checks,
        raw_sink=raw_sink if args.raw else None, stop_after=stop_after,
    )
    if combined is None:
        print("Could not fetch any requested date.", file=sys.stderr)
        return 2

    # Aggregating all days at once means a station-hour that straddles two
    # service dates (late-night service past midnight) becomes a single row.
    panel = station_hour_panel(combined)

    if stop_after:
        start = end = loaded[0]
    blob_name = f"{prefix}/processed/mbta_station_hour_panel_{panel_label(start, end)}.csv"

    # --- Upload the panel straight from memory -----------------------------------------
    print()
    try:
        upload_bytes(bucket, blob_name, panel.to_csv(index=False), "text/csv")
    except GoogleAPIError as exc:
        print(f"Panel upload FAILED: {exc}", file=sys.stderr)
        return 2

    # --- Summary -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"SUMMARY  {start} .. {end}")
    print(f"  dates loaded:  {len(loaded)} of {len(days) if not stop_after else 1}")
    if skipped and not stop_after:
        print(f"  dates skipped (not published or failed to process; see log above): "
              f"{', '.join(d.isoformat() for d in skipped)}")
    if not args.no_checks:
        if failed_checks:
            print("  dates with failed checks:")
            for d, names in failed_checks.items():
                print(f"    {d}: {', '.join(names)}")
        else:
            print("  all loaded dates passed every check")
    print(f"  panel rows: {len(panel):,}  "
          f"({panel['parent_station'].nunique()} stations x {panel['hour_local'].nunique()} hours)")
    print(f"  uploaded:   gs://{bucket.name}/{blob_name}")
    if args.raw:
        print(f"  raw files:  {len(loaded) - len(raw_failures)} uploaded to gs://{bucket.name}/{prefix}/raw/"
              + (f", {len(raw_failures)} FAILED: {', '.join(raw_failures)}" if raw_failures else ""))
    print("=" * 78)
    return 1 if raw_failures else 0


if __name__ == "__main__":
    sys.exit(main())
