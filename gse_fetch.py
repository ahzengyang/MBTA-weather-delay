"""
Step 1: pull raw MBTA Gated Station Entries (GSE).

Dataset: MBTA Gated Station Entries
https://mbta-massdot.opendata.arcgis.com/datasets/001c177f07594e7c99f193dde32284c9

Returns raw rows with these columns:
    service_date (local Eastern date, naive), time_period, stop_id,
    station_name, route_or_line, gated_entries, ObjectId

Notes
    - service_date is stored as local midnight expressed in UTC
      (04:00Z in EDT, 05:00Z in EST). We convert it to the Eastern date.
    - A service day runs 03:00 -> 02:59. So the 00:00-02:59 hours of
      calendar day D are filed under service date D-1. When you ask for
      calendar dates, we pull service dates D-1 and D, then gse_clean
      trims back to exactly the dates you asked for.
    - Server max is 2000 rows/request, so we query one service date at a
      time (~2-3k rows each) and run several in parallel.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GSE_URL = (
    "https://services1.arcgis.com/ceiitspzDAHrdGO1/arcgis/rest/services/"
    "GSE/FeatureServer/0/query"
)
HEADERS = {"User-Agent": "transit-delay-weather-class-project/1.0"}
TZ = "America/New_York"
PAGE = 2000
FIELDS = ["service_date", "time_period", "stop_id", "station_name",
          "route_or_line", "gated_entries", "ObjectId"]


# ---------------------------------------------------------------- dates
def to_dates(dates: Iterable) -> list[pd.Timestamp]:
    return sorted({pd.Timestamp(d).normalize() for d in dates})


def date_range(start: str, end: str) -> list[pd.Timestamp]:
    return list(pd.date_range(start, end, freq="D"))


def service_dates_for(calendar_dates: Iterable) -> list[pd.Timestamp]:
    """Service dates needed to cover these calendar dates (D-1 and D)."""
    one = pd.Timedelta(days=1)
    cal = to_dates(calendar_dates)
    return sorted(set(cal) | {d - one for d in cal})


# ---------------------------------------------------------------- API
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=5, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    return s


def _utc_ms_to_local_date(s: pd.Series) -> pd.Series:
    return (pd.to_datetime(s, unit="ms", utc=True)
            .dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize())


def api_date_range(session: requests.Session | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """First and last service date currently published on the API."""
    stats = ('[{"statisticType":"min","onStatisticField":"service_date","outStatisticFieldName":"lo"},'
             '{"statisticType":"max","onStatisticField":"service_date","outStatisticFieldName":"hi"}]')
    r = (session or _session()).get(GSE_URL, timeout=60, params={
        "f": "json", "where": "1=1", "outStatistics": stats})
    r.raise_for_status()
    a = r.json()["features"][0]["attributes"]
    lo, hi = _utc_ms_to_local_date(pd.Series([a["lo"], a["hi"]]))
    return lo, hi


def local_date_range(path: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """First and last service date in a downloaded GSE.csv."""
    s = pd.read_csv(path, encoding="utf-8-sig", usecols=["service_date"])["service_date"]
    d = (pd.to_datetime(s, format="%Y/%m/%d %H:%M:%S%z", utc=True)
         .dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize())
    return d.min(), d.max()


def available_range(local: str | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    return local_date_range(local) if local else api_date_range()


def _fetch_one_date(session: requests.Session, d: pd.Timestamp) -> list[dict]:
    lo = d.strftime("%Y-%m-%d")
    hi = (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    where = (f"service_date >= TIMESTAMP '{lo} 00:00:00' "
             f"AND service_date < TIMESTAMP '{hi} 00:00:00'")
    rows, offset = [], 0
    while True:
        r = session.get(GSE_URL, timeout=120, params={
            "f": "json", "where": where, "outFields": ",".join(FIELDS),
            "orderByFields": "ObjectId", "resultOffset": offset,
            "resultRecordCount": PAGE, "returnGeometry": "false",
        })
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"{lo}: {data['error']}")
        feats = data.get("features", [])
        rows.extend(f["attributes"] for f in feats)
        if not feats or not data.get("exceededTransferLimit"):
            return rows
        offset += len(feats)


def fetch_api(calendar_dates: Iterable, workers: int = 8) -> pd.DataFrame:
    """Raw GSE rows covering the given calendar dates."""
    svc = service_dates_for(calendar_dates)
    print(f"Fetching {len(svc)} service dates from the API")
    s = _session()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (d, chunk) in enumerate(zip(svc, ex.map(lambda d: _fetch_one_date(s, d), svc)), 1):
            if not chunk:
                print(f"  WARNING: no data for service date {d.date()}")
            rows.extend(chunk)
            if i % 50 == 0 or i == len(svc):
                print(f"  {i}/{len(svc)} dates, {len(rows):,} rows")

    df = pd.DataFrame(rows, columns=FIELDS)
    df["service_date"] = _utc_ms_to_local_date(df["service_date"])
    return df


# ---------------------------------------------------------------- local CSV
def load_local(path: str, calendar_dates: Iterable) -> pd.DataFrame:
    """Same output as fetch_api, from a downloaded GSE.csv."""
    df = pd.read_csv(path, encoding="utf-8-sig", usecols=FIELDS)
    df["service_date"] = (
        pd.to_datetime(df["service_date"], format="%Y/%m/%d %H:%M:%S%z", utc=True)
        .dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize()
    )
    svc = service_dates_for(calendar_dates)
    missing = sorted(set(svc) - set(df["service_date"].unique()))
    if missing:
        print(f"WARNING: {len(missing)} service date(s) not in {path}: "
              f"{[str(d.date()) for d in missing[:10]]}{' ...' if len(missing) > 10 else ''}")
    return df[df["service_date"].isin(svc)].reset_index(drop=True)


def load(calendar_dates: Iterable, local: str | None = None, workers: int = 8) -> pd.DataFrame:
    return load_local(local, calendar_dates) if local else fetch_api(calendar_dates, workers)
