"""
Gated station entries for storm hours.

Joins Alex's storm_hours.csv (one row per local calendar date + hour) with
MBTA Gated Station Entries (GSE), rolled up from 30-min periods to hours.

Output: one row per  date, hour, stop_id, station_name, line
        with gated_entries + that hour's weather columns.

Run:
    python storm_entries.py                 # pulls from the ArcGIS API
    python storm_entries.py --local GSE.csv # uses your downloaded GSE.csv instead

GOTCHAS
    - GSE "service_date" runs 03:00 -> 02:59 the next morning. So the
      00:00-02:59 hours of calendar day D live under service_date D-1.
      We fetch D-1 and D for every storm day and convert to calendar time.
    - The live API only covers ~2 years (currently 2024-08-25 .. 2026-06-30).
      Storm days outside that window are dropped (with a printed warning).
    - gated_entries arrives as a string; transfer stations are split by line
      with fractional values (e.g. Park St Red vs Green).
    - Hours with no row (station closed, ~03:00-04:59) are simply absent.
"""

from __future__ import annotations

import argparse

import pandas as pd
import requests

GSE_URL = (
    "https://services1.arcgis.com/ceiitspzDAHrdGO1/arcgis/rest/services/"
    "GSE/FeatureServer/0/query"
)
STORM_URL = (
    "https://raw.githubusercontent.com/ahzengyang/MBTA-weather-delay/"
    "AlexCasella/storm_hours.csv"
)
HEADERS = {"User-Agent": "transit-delay-weather-class-project/1.0"}
TZ = "America/New_York"
PAGE = 2000  # server max per request
FIELDS = "service_date,time_period,stop_id,station_name,route_or_line,gated_entries"


# ---------------------------------------------------------------- storm hours
def load_storm_hours(path_or_url: str = STORM_URL) -> pd.DataFrame:
    storm = pd.read_csv(path_or_url)
    storm["date"] = pd.to_datetime(storm["day"]).dt.normalize()
    storm["hour"] = storm["hour_local"].astype(int)
    return storm.drop(columns=["day", "hour_local"])


# ---------------------------------------------------------------- GSE fetch
def _query(where: str) -> list[dict]:
    rows, offset = [], 0
    while True:
        params = {
            "f": "json",
            "where": where,
            "outFields": FIELDS,
            "orderByFields": "ObjectId",
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "returnGeometry": "false",
        }
        r = requests.get(GSE_URL, params=params, headers=HEADERS, timeout=120)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        feats = data.get("features", [])
        rows.extend(f["attributes"] for f in feats)
        if not feats or not data.get("exceededTransferLimit"):
            return rows
        offset += len(feats)


def api_date_range() -> tuple[pd.Timestamp, pd.Timestamp]:
    """Min/max service_date currently published on the API."""
    stats = (
        '[{"statisticType":"min","onStatisticField":"service_date","outStatisticFieldName":"lo"},'
        '{"statisticType":"max","onStatisticField":"service_date","outStatisticFieldName":"hi"}]'
    )
    r = requests.get(
        GSE_URL,
        params={"f": "json", "where": "1=1", "outStatistics": stats},
        headers=HEADERS, timeout=60,
    )
    r.raise_for_status()
    a = r.json()["features"][0]["attributes"]
    conv = lambda ms: pd.to_datetime(ms, unit="ms", utc=True).tz_convert(TZ).tz_localize(None).normalize()
    return conv(a["lo"]), conv(a["hi"])


def fetch_service_dates(dates: list[pd.Timestamp]) -> pd.DataFrame:
    """Raw GSE rows for the given service dates (local dates).

    service_date is stored as local midnight in UTC (04:00/05:00Z), so a
    UTC window [D 00:00Z, D+1 00:00Z) captures exactly service date D.
    """
    frames = []
    for d in sorted(set(dates)):
        lo, hi = d.strftime("%Y-%m-%d"), (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        where = f"service_date >= TIMESTAMP '{lo} 00:00:00' AND service_date < TIMESTAMP '{hi} 00:00:00'"
        rows = _query(where)
        print(f"  service_date {lo}: {len(rows):,} rows")
        frames.append(pd.DataFrame(rows))
    df = pd.concat(frames, ignore_index=True)
    df["service_date"] = (
        pd.to_datetime(df["service_date"], unit="ms", utc=True)
        .dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize()
    )
    return df


def load_local_gse(path: str, dates: list[pd.Timestamp]) -> pd.DataFrame:
    """Same as fetch_service_dates, but from a downloaded GSE.csv."""
    df = pd.read_csv(path, encoding="utf-8-sig", usecols=FIELDS.split(","))
    df["service_date"] = (
        pd.to_datetime(df["service_date"], format="%Y/%m/%d %H:%M:%S%z", utc=True)
        .dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize()
    )
    return df[df["service_date"].isin(set(dates))].reset_index(drop=True)


# ---------------------------------------------------------------- transform
def to_hourly(gse: pd.DataFrame) -> pd.DataFrame:
    """30-min service-day periods -> calendar date + hour, summed."""
    df = gse.copy()
    df["gated_entries"] = pd.to_numeric(df["gated_entries"], errors="coerce")
    t = pd.to_timedelta(df["time_period"].astype(str).str.strip("()"))
    # 00:00-02:59 periods belong to the morning AFTER the service date
    t = t.where(t >= pd.Timedelta(hours=3), t + pd.Timedelta(days=1))
    start = df["service_date"] + t
    df["date"] = start.dt.normalize()
    df["hour"] = start.dt.hour
    return (
        df.rename(columns={"route_or_line": "line"})
        .groupby(["date", "hour", "stop_id", "station_name", "line"], as_index=False)
        ["gated_entries"].sum()
    )


def build(storm: pd.DataFrame, local_gse: str | None = None) -> pd.DataFrame:
    days = [pd.Timestamp(d) for d in sorted(storm["date"].unique())]
    one = pd.Timedelta(days=1)
    needed_all = sorted({d - one for d in days} | set(days))

    if local_gse:
        raw = load_local_gse(local_gse, needed_all)
        lo, hi = raw["service_date"].min(), raw["service_date"].max()
        src = "Local file"
    else:
        lo, hi = api_date_range()
        src = "API"
    print(f"{src} covers {lo.date()} .. {hi.date()}")

    # keep storm days whose service dates (D-1 and D) are both available
    keep = [d for d in days if d - one >= lo and d <= hi]
    dropped = [str(d.date()) for d in days if d not in keep]
    if dropped:
        print(f"Dropping {len(dropped)} storm day(s) outside window: {dropped}")

    needed = sorted({d - one for d in keep} | set(keep))
    if not local_gse:
        raw = fetch_service_dates(needed)
    hourly = to_hourly(raw[raw["service_date"].isin(needed)])

    out = hourly.merge(storm[storm["date"].isin(keep)], on=["date", "hour"], how="inner")

    cols = ["date", "hour", "stop_id", "station_name", "line", "gated_entries"]
    rest = [c for c in out.columns if c not in cols]
    return out[cols + rest].sort_values(["date", "hour", "station_name", "line"]).reset_index(drop=True)


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--storm", default=STORM_URL, help="storm_hours.csv path or URL")
    p.add_argument("--local", default=None, help="use a downloaded GSE.csv instead of the API")
    p.add_argument("--out", default="storm_entries.csv")
    a = p.parse_args()

    storm = load_storm_hours(a.storm)
    df = build(storm, a.local)
    df.to_csv(a.out, index=False)
    print(f"Saved {len(df):,} rows -> {a.out}")
    print(df.head())


if __name__ == "__main__":
    main()
