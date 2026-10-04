"""Grab all MBTA subway stations and latitude/longitude coordinates from the MBTA API.

No API key was needed at low request volume. route_type 0 = light rail which is green and
Mattapan. route_type 1 = heavy rail which is red, orange, and blue.
"""
import time
import pandas as pd
import requests

BASE = "https://api-v3.mbta.com"

routes = requests.get(f"{BASE}/routes", params={"filter[type]": "0,1"}, timeout=30).json()["data"]
print(f"subway routes: {[r['id'] for r in routes]}\n")

rows = []
for r in routes:
    rid = r["id"]
    line = r["attributes"]["long_name"]
    resp = requests.get(
        f"{BASE}/stops",
        params={"filter[route]": rid, "filter[location_type]": 1},
        timeout=30,
    ).json()["data"]
    for s in resp:
        a = s["attributes"]
        rows.append({
            "station_id": s["id"],
            "station_name": a["name"],
            "latitude": a["latitude"],
            "longitude": a["longitude"],
            "municipality": a.get("municipality"),
            "line": line,
        })
    time.sleep(0.2)

df = pd.DataFrame(rows)
stations = (
    df.groupby(["station_id", "station_name", "latitude", "longitude", "municipality"],
               as_index=False)
      .agg(lines=("line", lambda s: ", ".join(sorted(set(s)))))
      .sort_values("station_name")
      .reset_index(drop=True)
)
stations["is_underground"] = ""   #wait for Angela to fill in
stations.to_csv("stations.csv", index=False)
print(f"{len(df)} station-route rows -> {len(stations)} distinct parent stations\n")
print(stations[["station_name", "latitude", "longitude", "municipality", "lines"]].to_string())

heavy = stations[stations["lines"].str.contains("Red Line|Orange Line|Blue Line", na=False)]
heavy.to_csv("stations_heavy_rail.csv", index=False)
print(f"\nheavy rail only: {len(heavy)} stations -> stations_heavy_rail.csv")
