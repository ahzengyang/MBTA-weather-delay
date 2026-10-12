""" 
fastapi/mbta_stations_api.py — api wrapper of the station_data.py script 

- GET /health - return status
- GET /stations — runs the full pipeline and returns the resulting processed df as CSV 
- POST /stations/upload — just uploads the result to GCS 

"""

