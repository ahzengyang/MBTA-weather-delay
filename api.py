"""
FastAPI wrapper around storm_entries.py

Run:
    pip install fastapi uvicorn
    fastapi run api.py            # or: uvicorn api:app --reload

Endpoints:
    GET /storm-entries                 JSON rows
        ?severe_only=true              only severe hours
        &line=Red Line                 filter by line
        &station=place-sstat           filter by stop_id
        &date=2025-03-17               filter by date
    GET /storm-entries.csv             same filters, CSV download
    POST /refresh                      re-pull from the API (clears cache)
"""

from __future__ import annotations

import io
from functools import lru_cache

import pandas as pd
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse

from storm_entries import build, load_storm_hours

app = FastAPI(title="MBTA storm-hour gated entries")


@lru_cache(maxsize=1)
def _data() -> pd.DataFrame:
    # First call hits the ArcGIS API (~30 requests); later calls are cached.
    return build(load_storm_hours())


def _filter(severe_only, line, station, date) -> pd.DataFrame:
    df = _data()
    if severe_only:
        df = df[df["severe"]]
    if line:
        df = df[df["line"] == line]
    if station:
        df = df[df["stop_id"] == station]
    if date:
        df = df[df["date"] == pd.Timestamp(date)]
    return df


@app.get("/storm-entries")
def storm_entries(
    severe_only: bool = False,
    line: str | None = Query(None, examples=["Red Line"]),
    station: str | None = Query(None, examples=["place-sstat"]),
    date: str | None = Query(None, examples=["2025-03-17"]),
):
    df = _filter(severe_only, line, station, date).copy()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    # NaN -> None so it serializes as JSON null
    return df.astype(object).where(df.notna(), None).to_dict(orient="records")


@app.get("/storm-entries.csv")
def storm_entries_csv(
    severe_only: bool = False,
    line: str | None = None,
    station: str | None = None,
    date: str | None = None,
):
    buf = io.StringIO()
    _filter(severe_only, line, station, date).to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=storm_entries.csv"},
    )


@app.post("/refresh")
def refresh():
    _data.cache_clear()
    return {"rows": len(_data())}
