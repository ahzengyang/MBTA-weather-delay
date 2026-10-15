"""
Step 1:
Pull MBTA Gated Station Entries from URL.

Dataset: MBTA Gated Station Entries
https://mbta-massdot.opendata.arcgis.com/datasets/001c177f07594e7c99f193dde32284c9

Returns rows with columns:
service_date (local Boston date/time), time_period, stop_id,
station_name, route_or_line, gated_entries, ObjectId

Converted time to EST zone, accounting for daylight savings in november.
"""

import pandas as pd
import requests

GSE_URL = (
    "https://services1.arcgis.com/ceiitspzDAHrdGO1/arcgis/rest/services/"
    "GSE/FeatureServer/0/query"
)
HEADERS = {"User-Agent": "transit-delay-weather-class-project/1.0"}
TZ = "America/New_York"
PAGE = 2000
FIELDS = ["service_date", "time_period", "stop_id", "station_name",
          "route_or_line", "gated_entries", "ObjectId"]


def to_dates(dates):
    return sorted({pd.Timestamp(d).normalize() for d in dates})


def date_range(start, end):
    return list(pd.date_range(start, end, freq="D"))


def service_dates_for(calendar_dates):
    # a service day runs 3am to 3am, so calendar date D also needs service date D-1
    cal = to_dates(calendar_dates)
    return sorted(set(cal) | {d - pd.Timedelta(days=1) for d in cal})


def _to_local_date(utc):
    return utc.dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize()


def _api_edge_date(order):
    r = requests.get(GSE_URL, headers=HEADERS, timeout=60, params={
        "f": "json", "where": "1=1", "outFields": "service_date",
        "orderByFields": f"service_date {order}", "resultRecordCount": 1})
    r.raise_for_status()
    ms = r.json()["features"][0]["attributes"]["service_date"]
    return _to_local_date(pd.to_datetime(pd.Series([ms]), unit="ms", utc=True))[0]


def _fetch_one_date(d):
    lo = d.strftime("%Y-%m-%d")
    hi = (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    where = f"service_date >= TIMESTAMP '{lo} 00:00:00' AND service_date < TIMESTAMP '{hi} 00:00:00'"
    rows = []
    while True:
        r = requests.get(GSE_URL, headers=HEADERS, timeout=120, params={
            "f": "json", "where": where, "outFields": ",".join(FIELDS),
            "orderByFields": "ObjectId", "resultOffset": len(rows),
            "resultRecordCount": PAGE, "returnGeometry": "false"})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"{lo}: {data['error']}")
        features = data.get("features", [])
        rows += [f["attributes"] for f in features]
        if not features or not data.get("exceededTransferLimit"):
            return rows


def fetch_api(calendar_dates):
    svc = service_dates_for(calendar_dates)
    print(f"Fetching {len(svc)} service dates from the API")
    rows = []
    for i, d in enumerate(svc, 1):
        rows += _fetch_one_date(d)
        if i % 50 == 0 or i == len(svc):
            print(f"  {i}/{len(svc)} dates, {len(rows):,} rows")
    df = pd.DataFrame(rows, columns=FIELDS)
    df["service_date"] = _to_local_date(pd.to_datetime(df["service_date"], unit="ms", utc=True))
    return df


def _read_local(path, columns):
    df = pd.read_csv(path, encoding="utf-8-sig", usecols=columns)
    df["service_date"] = _to_local_date(
        pd.to_datetime(df["service_date"], format="%Y/%m/%d %H:%M:%S%z", utc=True))
    return df


def available_range(local=None):
    """First and last service date, from a downloaded GSE.csv or the API."""
    if local:
        d = _read_local(local, ["service_date"])["service_date"]
        return d.min(), d.max()
    return _api_edge_date("ASC"), _api_edge_date("DESC")


def load(calendar_dates, local=None):
    if not local:
        return fetch_api(calendar_dates)
    df = _read_local(local, FIELDS)
    return df[df["service_date"].isin(service_dates_for(calendar_dates))].reset_index(drop=True)
