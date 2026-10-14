# =====================================================================
# mbta-weather-delay -- shared deployment configuration
#
# Sourced by gcloud_command.sh:
#   source "$(dirname "$0")/config.sh"
# =====================================================================

# This file's directory, i.e. the repo root.
CONFIG_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Personal overrides, if present. Only ENV_FILE and LOCAL_KEY_FILE can
# be overridden here -- they use the ${VAR:-default} form below. Every
# other assignment in this file is unconditional and will overwrite
# anything config.local.sh sets.
[ -f "$CONFIG_DIR/config.local.sh" ] && source "$CONFIG_DIR/config.local.sh"

# ---------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------

PROJECT_ID="mbta-weather-delay"
REGION="us-west1"
GCS_BUCKET="mbta-weather-delay"

# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------

# This repo's .env, created by copying .env_template. Repo-relative, so
# no one has to edit a path to deploy.
ENV_FILE="${ENV_FILE:-$CONFIG_DIR/.env}"

LOCAL_KEY_FILE="${LOCAL_KEY_FILE:-$HOME/.ssh/mbta-weather-delay-d8fdc30c97c0.json}"

# Directories holding each Dockerfile, relative to the repo root.
API_DIR="fastapi"
WEBAPP_DIR="streamlit"

# Ports your two Dockerfiles EXPOSE.
API_PORT="8000"
WEBAPP_PORT="8501"

# ---------------------------------------------------------------------
# Resource names
# ---------------------------------------------------------------------

GCP_KEY_SECRET="mbta-weather-gcp-key"
SECRET_MOUNT_PATH="/run/secrets/gcp_key"

# Cloud Run service names.
API_SERVICE="mbta-weather-api"
WEBAPP_SERVICE="mbta-weather-dashboard"

# =====================================================================
# Cloud Scheduler jobs -- one per data source, plus the transform.
#
# Format, pipe-delimited:   name | endpoint | cron | request body
#
# Cadences match how often each source actually changes, and how much
# history a single call can retrieve:
#
#   stations   yearly   -- GTFS stop definitions are relatively static;
#                          names and IDs we join on effectively never
#                          move. Runs first so reference data is in
#                          place before the day's collectors.
#   weather    daily    -- Open-Meteo publishes hourly, but one call
#                          with past_days returns the whole previous
#                          day of hourly rows, so a daily pull loses
#                          no resolution.
#   delays     daily    -- MBTA performance files publish on a lag of
#                          roughly two days, so polling faster than
#                          daily returns the same files again.
#   ridership  monthly  -- gated entry counts are republished in
#                          monthly batches.
#   transform  daily, 15 minutes behind the last collector of the day,
#              so it reads a complete set of raw files rather than
#              racing a collector that is still writing.
#
# Cloud Scheduler's free tier covers 3 jobs; 5 costs a few cents a month.
# =====================================================================
SCHEDULER_JOB_PREFIX="mbta-collect"
SCHEDULER_TZ="America/New_York"

SCHEDULE_SPECS="
stations|/collect/stations|0 4 1 1 *|{}
weather|/collect/weather|0 5 * * *|{}
delays|/collect/delays|30 6 * * *|{}
ridership|/collect/ridership|0 7 1 * *|{}
transform|/transform|15 7 * * *|{}
"

# =====================================================================
# Validation
# =====================================================================
# Only the two checks that stop the deploy from working at all, and
# that a teammate will realistically hit on a fresh clone.

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: ENV_FILE '$ENV_FILE' does not exist." >&2
  echo "       Create it from the template:" >&2
  echo "         cp $CONFIG_DIR/.env_template $CONFIG_DIR/.env" >&2
  echo "       then fill in the values." >&2
  exit 1
fi

if [ ! -f "$LOCAL_KEY_FILE" ]; then
  echo "ERROR: LOCAL_KEY_FILE '$LOCAL_KEY_FILE' does not exist." >&2
  echo "       Your key is probably at a different path. Set it in" >&2
  echo "       $CONFIG_DIR/config.local.sh:" >&2
  echo "         LOCAL_KEY_FILE=\"\$HOME/path/to/your-key.json\"" >&2
  exit 1
fi
