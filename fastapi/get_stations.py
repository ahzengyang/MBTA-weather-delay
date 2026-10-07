"""Grab all MBTA subway stations and latitude/longitude coordinates from the MBTA API.

No API key was needed at low request volume. route_type 0 = light rail which is green and
Mattapan. route_type 1 = heavy rail which is red, orange, and blue.
"""
import time
import pandas as pd
import requests
from user_definition import *

def get_routes() -> list:
    response = requests.get(f"{mbta_url}/routes",
                            params={"filter[type]": "0,1"},
                            timeout=30)
    return response.json()["data"]

def get_stations(route_id: str,
                 line_name: str) -> list:
    response = requests.get(f"{mbta_url}/stops",
                            params={"filter[route]": route_id,
                                    "filter[location_type]": 1},
                            timeout=30)
    rows = []
    for stop in response.json()["data"]:
        attributes = stop["attributes"]
        rows.append({"station_id": stop["id"],
                     "station_name": attributes["name"],
                     "latitude": attributes["latitude"],
                     "longitude": attributes["longitude"],
                     "municipality": attributes.get("municipality"),
                     "line": line_name})
    return rows

if __name__ == '__main__':
    routes = get_routes()
    print(f"subway routes: {[route['id'] for route in routes]}")

    rows = []
    for route in routes:
        rows += get_stations(route["id"],
                             route["attributes"]["long_name"])
        time.sleep(0.2)

    data = pd.DataFrame(rows)
    stations = (data.groupby(["station_id", "station_name", "latitude",
                              "longitude", "municipality"], as_index=False)
                .agg(lines=("line", lambda line: ", ".join(sorted(set(line)))))
                .sort_values("station_name")
                .reset_index(drop=True))
    stations["is_underground"] = ""
    stations.to_csv(stations_file, index=False)
    print(f"{len(data)} station-route rows -> "
          f"{len(stations)} distinct parent stations")

    heavy = stations[stations["lines"].str.contains(
        "Red Line|Orange Line|Blue Line", na=False)]
    heavy.to_csv(heavy_rail_file, index=False)
    print(f"heavy rail only: {len(heavy)} stations -> {heavy_rail_file}")
