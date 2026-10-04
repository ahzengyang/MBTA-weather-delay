
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


# ------------------------- Configuration
BASE_URL = "https://performancedata.mbta.com/lamp/subway-on-time-performance-v1"
INDEX_URL = f"{BASE_URL}/index.csv"
FILE_TEMPLATE = "{d}-subway-on-time-performance-v1.parquet"
LOCAL_TZ = "America/New_York"
HTTP_TIMEOUT_S = 180
USER_AGENT = "mbta-weather-delay-class-project/1.0 (python-requests)"

# GCS upload settings
DEFAULT_PREFIX = "mbta_lamp"
UPLOAD_TIMEOUT_S = 300
UPLOAD_RETRY = DEFAULT_RETRY.with_deadline(600)
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

NUMERIC_COLUMNS = (
    "stop_sequence",
    "stop_timestamp",
    "travel_time_seconds",
    "scheduled_travel_time",
    "headway_trunk_seconds",
    "scheduled_headway_trunk",
)

# An excess this large is a data error (mis-matched trip, vehicle held out of
# service, merged segments), not a weather effect.
IMPLAUSIBLE_ABS_EXCESS_S = 60 * 60

# stop_timestamp outside this window (2014-05 .. 2039-09) cannot be POSIX seconds
PLAUSIBLE_TS_RANGE = (1.4e9, 2.2e9)

# Only these derived columns are needed by station_hour_panel(). Keeping just
# these per day keeps memory small when a range spans weeks or months.
PANEL_INPUT_COLUMNS = [
    "parent_station", "hour_local", "hour_utc",
    "usable", "excess_travel_min",
    "usable_headway", "excess_headway_min",
]


# -------------------------
# Small helpers
# -------------------------
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
    """Convert to plain float64, turning anything non-numeric (including pandas NA) into NaN."""
    num = pd.to_numeric(s, errors="coerce")
    return pd.Series(num.to_numpy(dtype="float64", na_value=np.nan), index=s.index, name=s.name)


def _service_date_as_date(s: pd.Series) -> pd.Series:
    """Parse dates written as YYYYMMDD or YYYY-MM-DD; unparseable values become NaT."""
    text = s.astype("string").str.strip().str.slice(0, 10).str.replace("-", "", regex=False)
    return pd.to_datetime(text, format="%Y%m%d", errors="coerce").dt.date


# -------------------------
# Acquisition
# -------------------------
def _parse_index_text(text: str) -> list[date]:
    """Extract service dates from index.csv text without assuming its exact layout."""
    found: set[date] = set()

    # 1) Read it as a CSV and parse any column whose name mentions "date".
    try:
        index = pd.read_csv(io.StringIO(text), dtype=str)
        date_columns = [col for col in index.columns if "date" in str(col).lower()]
        for col in date_columns:
            found.update(_service_date_as_date(index[col]).dropna())
    except Exception:  # not a readable CSV; the text searches below still apply
        pass

    # 2) Dates that appear in the Parquet file names.
    for match in re.findall(r"(\d{4}-\d{2}-\d{2})-subway-on-time-performance", text):
        found.add(_as_date(match))

    # 3) Last resort: any YYYY-MM-DD anywhere in the text.
    if not found:
        for match in re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text):
            try:
                found.add(_as_date(match))
            except ValueError:
                continue

    # Ignore anything outside the plausible publishing window.
    earliest = date(2019, 1, 1)
    latest = datetime.now(ZoneInfo(LOCAL_TZ)).date() + timedelta(days=1)
    return sorted(d for d in found if earliest <= d <= latest)


def list_service_dates(session: requests.Session | None = None) -> list[date]:
    """Return the published service dates listed in LAMP's index.csv, ascending."""
    sess = session or _session()
    resp = sess.get(INDEX_URL, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()

    content = resp.content
    if content[:2] == b"\x1f\x8b":  # gzip magic bytes: file stored compressed
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
    return pd.read_parquet(io.BytesIO(resp.content), engine="pyarrow")


# -------------------------
# Derivation
# -------------------------
def _is_valid_measure(actual: pd.Series, scheduled: pd.Series, excess: pd.Series) -> pd.Series:
    """True where actual and scheduled are both > 0 (so not missing) and the
    excess is within the plausible limit."""
    return actual.gt(0) & scheduled.gt(0) & excess.abs().le(IMPLAUSIBLE_ABS_EXCESS_S)


def add_excess_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Add excess times, the local hour of each arrival, and usable flags.
    Never drops rows; returns a copy sorted by (service_date, trip_id, stop_sequence).

    Added columns
      excess_travel_s / excess_travel_min    actual - scheduled travel time
      excess_headway_s / excess_headway_min  actual - scheduled headway
      hour_utc, hour_local                   arrival hour, from stop_timestamp
      usable                                 row counts toward travel statistics
      usable_headway                         row counts toward headway statistics
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise KeyError(f"LAMP frame is missing expected columns: {missing}")

    df = raw.copy()
    for col in NUMERIC_COLUMNS:
        df[col] = _to_float(df[col])

    # Some files contain stop_timestamp values far outside POSIX-seconds range,
    # which would crash the datetime conversion. Treat them as missing.
    ts = df["stop_timestamp"]
    bad_ts = ts.notna() & ~ts.between(*PLAUSIBLE_TS_RANGE)
    if bad_ts.any():
        print(f"  WARNING: {int(bad_ts.sum()):,} stop_timestamp value(s) outside the plausible "
              f"POSIX-seconds range set to missing (examples: {ts[bad_ts].head(3).tolist()})")
        df.loc[bad_ts, "stop_timestamp"] = np.nan

    # Keep rows in trip order.
    df = df.sort_values(
        TRIP_KEY + ["stop_sequence", "stop_timestamp"],
        kind="mergesort", na_position="last",
    ).reset_index(drop=True)

    # --- Excess times ------------------------------------------------------------
    df["excess_travel_s"] = df["travel_time_seconds"] - df["scheduled_travel_time"]
    df["excess_travel_min"] = df["excess_travel_s"] / 60.0
    df["excess_headway_s"] = df["headway_trunk_seconds"] - df["scheduled_headway_trunk"]
    df["excess_headway_min"] = df["excess_headway_s"] / 60.0

    # --- Arrival hour --------------------------------------------------------------
    # Floor to the hour in UTC, then convert. EST/EDT offsets are whole hours, so
    # this matches local hours and avoids ambiguous times on the fall-back night.
    arrival_utc = pd.to_datetime(df["stop_timestamp"], unit="s", utc=True, errors="coerce")
    df["hour_utc"] = arrival_utc.dt.floor("h")
    df["hour_local"] = df["hour_utc"].dt.tz_convert(LOCAL_TZ)

    # --- Which rows count -----------------------------------------------------------
    can_place = df["hour_utc"].notna() & df["parent_station"].notna()
    df["usable"] = can_place & _is_valid_measure(
        df["travel_time_seconds"], df["scheduled_travel_time"], df["excess_travel_s"])
    df["usable_headway"] = can_place & _is_valid_measure(
        df["headway_trunk_seconds"], df["scheduled_headway_trunk"], df["excess_headway_s"])
    return df


# -------------------------
# Aggregation
# -------------------------
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

    has_key = df["hour_local"].notna() & df["parent_station"].notna()
    arrivals = df[has_key].groupby(keys, observed=True).size().rename("n_arrivals")

    travel = df[df["usable"]].groupby(keys, observed=True)["excess_travel_min"].agg(
        n_travel_obs="count",
        excess_travel_min_median="median",
        excess_travel_min_mean="mean",
        excess_travel_min_p90=_p90,
        excess_travel_min_sum="sum",
    )

    headway = df[df["usable_headway"]].groupby(keys, observed=True)["excess_headway_min"].agg(
        n_headway_obs="count",
        excess_headway_min_median="median",
    )

    panel = pd.concat([arrivals, travel, headway], axis=1)

    # A station-hour missing from one of the groups had zero observations there.
    count_cols = ["n_arrivals", "n_travel_obs", "n_headway_obs"]
    panel[count_cols] = panel[count_cols].fillna(0).astype("int64")

    panel = panel.reset_index()
    panel.insert(2, "hour_utc", panel["hour_local"].dt.tz_convert("UTC"))
    return panel.sort_values(keys).reset_index(drop=True)


# -------------------------
# Google Cloud Storage upload
# -------------------------
def check_gcp_config() -> None:
    """Fail early, before any download, if user_definition.py didn't load the env file."""
    settings = {
        "GCP_PROJECT_ID": project_id,
        "GCP_BUCKET_NAME": bucket_name,
        "GCP_SERVICE_ACCOUNT_KEY": service_account_file_path,
    }
    missing = [name for name, value in settings.items() if not value]
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


# Main -------------------------
def _candidate_dates(dates: list[date], min_age_days: int) -> list[date]:
    """Most recent dates first, skipping ones too new to be complete (today's file grows live)."""
    cutoff = datetime.now(ZoneInfo(LOCAL_TZ)).date() - timedelta(days=min_age_days)
    return sorted((d for d in dates if d <= cutoff), reverse=True)


def panel_label(start: date, end: date) -> str:
    """File-name label: 'YYYY-MM-DD' for one day, 'START_to_END' for a range."""
    return start.isoformat() if start == end else f"{start.isoformat()}_to_{end.isoformat()}"


def process_day(d: date, sess: requests.Session,
                raw_sink: Callable[[str, bytes], None] | None = None) -> pd.DataFrame:
    """
    Fetch and derive one service date; returns just the PANEL_INPUT_COLUMNS.
    Raises FileNotFoundError / requests.RequestException if the file can't be
    fetched, and any other exception if it can't be processed.
    """
    raw = fetch_service_date(d, sess, raw_sink=raw_sink)
    print(f"  Loaded {len(raw):,} rows x {raw.shape[1]} columns")
    df = add_excess_columns(raw)
    return df[PANEL_INPUT_COLUMNS].copy()


def collect(days: list[date], sess: requests.Session,
            raw_sink: Callable[[str, bytes], None] | None = None,
            stop_after: int | None = None):
    """
    Fetch and derive each date in `days`. Returns (combined frame or None,
    loaded_dates, skipped_dates). stop_after=1 takes the first date that loads.
    """
    frames: list[pd.DataFrame] = []
    loaded: list[date] = []
    skipped: list[date] = []

    for i, d in enumerate(days, 1):
        print(f"\n[{i}/{len(days)}] Fetching {d} ...")
        try:
            frames.append(process_day(d, sess, raw_sink))
        except (FileNotFoundError, requests.RequestException) as exc:
            print(f"  skipped: {exc}")
            skipped.append(d)
            continue
        except Exception as exc:  # one malformed file should not sink a long range
            print(f"  skipped: could not process {d}: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped.append(d)
            continue
        loaded.append(d)
        if stop_after and len(loaded) >= stop_after:
            break

    combined = pd.concat(frames, ignore_index=True) if frames else None
    return combined, loaded, skipped


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
    ap.add_argument("--min-age-days", type=int, default=2,
                    help="treat dates newer than this many days as possibly incomplete (default 2)")
    args = ap.parse_args(argv)
    prefix = args.prefix.strip("/")

# ------------------------- GCP setup first, so a bad config fails before any downloading
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

# ------------------------- Which dates 
    sess = _session()
    use_latest = not (args.range or args.date)

    if use_latest:
        published = list_service_dates(sess)
        print(f"index.csv lists {len(published):,} dates ({published[0]} .. {published[-1]})")
        days = _candidate_dates(published, args.min_age_days)[:5]
        if not days:
            print("No published date is old enough; rerun with --min-age-days 0.", file=sys.stderr)
            return 2
    else:
        first, last = args.range if args.range else (args.date, args.date)
        try:
            start, end = _as_date(first), _as_date(last)
        except ValueError:
            ap.error("dates must be YYYY-MM-DD")
        if end < start:
            ap.error(f"--range END ({end}) is before START ({start})")
        cutoff = datetime.now(ZoneInfo(LOCAL_TZ)).date() - timedelta(days=args.min_age_days)
        if end > cutoff:
            print(f"WARNING: dates after {cutoff} may be incomplete (files for recent days grow live).")
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    # ------------------------- Fetch, derive, aggregate
    combined, loaded, skipped = collect(
        days, sess,
        raw_sink=raw_sink if args.raw else None,
        stop_after=1 if use_latest else None,   # latest mode: newest date that loads
    )
    if combined is None:
        print("Could not fetch any requested date.", file=sys.stderr)
        return 2
    if use_latest:
        start = end = loaded[0]

    # Aggregating all days at once means a station-hour that straddles two
    # service dates (late-night service past midnight) becomes a single row.
    panel = station_hour_panel(combined)

# -------------------------Upload the panel straight from memory
    blob_name = f"{prefix}/processed/mbta_station_hour_panel_{panel_label(start, end)}.csv"
    print()
    try:
        upload_bytes(bucket, blob_name, panel.to_csv(index=False), "text/csv")
    except GoogleAPIError as exc:
        print(f"Panel upload FAILED: {exc}", file=sys.stderr)
        return 2

# ------------------------- Summary
    print("\n" + "=" * 78)
    print(f"SUMMARY  {start} .. {end}")
    print(f"  dates loaded:  {len(loaded)} of {1 if use_latest else len(days)}")
    if skipped and not use_latest:
        print(f"  dates skipped (not published or failed to process; see log above): "
              f"{', '.join(d.isoformat() for d in skipped)}")
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
