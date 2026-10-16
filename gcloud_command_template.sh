#!/usr/bin/env bash
#
# gcloud_command.sh — deploy and schedule this project on GCP.
#
#   ./gcloud_command.sh all        # everything, in order (first-time setup)
#   ./gcloud_command.sh bootstrap  # enable APIs, upload your key to Secret Manager
#   ./gcloud_command.sh api        # redeploy the API only
#   ./gcloud_command.sh web        # redeploy the web app only
#   ./gcloud_command.sh scheduler  # (re)create the Cloud Scheduler jobs only
#   ./gcloud_command.sh urls       # print the deployed URLs
#
# This deploys straight from source with `gcloud run deploy --source`, the
# same way HW3 did.

set -euo pipefail

# Run from the script's own directory so the relative paths below resolve
# the same way no matter where you invoke it from.
cd "$(dirname "${BASH_SOURCE[0]}")"

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PROJECT_ID="mbta-weather-delay"         # GCP project ID (not the display name)
REGION="us-west1"                       # keep the same region everywhere;

API_SERVICE="mbta-weather-api"          # Cloud Run service name for the API
WEB_SERVICE="mbta-weather-dashboard"    # Cloud Run service name for Streamlit

API_DIR="./fastapi"                     # folder holding the API Dockerfile
WEB_DIR="./streamlit"                   # folder holding the Streamlit Dockerfile

API_PORT=8000                           # must match EXPOSE/CMD in api/Dockerfile
WEB_PORT=8501                           # Streamlit default

ENV_FILE=".env"
LOCAL_KEY_FILE= #absolute path to service account JSON key on local machine

GCP_KEY_SECRET="mbta-weather-gcp-key"
SECRET_MOUNT_PATH="/run/secrets/gcp_key"

# ─── Scheduler jobs ───────────────────────────────────────────────────────────
# One job per data source. Format, pipe-delimited:
#
#   name | endpoint | cron | request body
#
# Endpoints are the real POST routes in the API. Query params go in the
# endpoint string.
#
# Cadences match how often each source actually changes:
#   stations   yearly   -- GTFS stop definitions are relatively static
#   weather    daily    -- /weather/daily writes yesterday to weather/processed/daily/
#   delays     daily    -- LAMP publishes on a lag; MIN_AGE_DAYS=2 means
#                          anything newer may still be growing. With no dates,
#                          the endpoint uploads the last PANEL_WINDOW_DAYS.
#   ridership  monthly  -- gated entry counts republish in monthly batches
#   combined   daily    -- runs after the daily sources have landed
#
# /weather/refresh overwrites the whole weather table; never schedule it.
SCHEDULER_TZ="America/New_York"         # the data is Boston local time

SCHEDULE_SPECS="
stations|/stations/upload|0 4 1 1 *|{}
weather|/weather/daily|0 5 * * *|{}
delays|/delays/panel/upload|30 6 * * *|{}
ridership|/ridership/refresh|0 7 1 * *|{}
combined|/combined/refresh|15 7 * * *|{}
"
# ─────────────────────────────────────────────────────────────────────────────

# ─── validation ──────────────────────────────────────────────────────────────
for _var in PROJECT_ID REGION API_SERVICE WEB_SERVICE API_DIR WEB_DIR \
            API_PORT WEB_PORT ENV_FILE LOCAL_KEY_FILE GCP_KEY_SECRET \
            SECRET_MOUNT_PATH SCHEDULER_TZ SCHEDULE_SPECS; do
  if [ -z "${!_var}" ]; then
    echo "ERROR: $_var is empty -- fill in the CONFIG block at the top of" \
         "this script before running it." >&2
    exit 1
  fi
done
unset _var

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: ENV_FILE '$ENV_FILE' does not exist." \
       "Copy .env_template to .env and fill it in." >&2
  exit 1
fi
if [ ! -f "$LOCAL_KEY_FILE" ]; then
  echo "ERROR: LOCAL_KEY_FILE '$LOCAL_KEY_FILE' does not exist." >&2
  echo "       Your key is probably at a different path. Run as:" >&2
  echo "         LOCAL_KEY_FILE=~/path/to/key.json $0 $*" >&2
  exit 1
fi
for _dir in "$API_DIR" "$WEB_DIR"; do
  if [ ! -f "$_dir/Dockerfile" ]; then
    echo "ERROR: no Dockerfile in '$_dir'." >&2
    exit 1
  fi
done
unset _dir

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

ENV_YAML=$(mktemp)
trap 'rm -f "$ENV_YAML"' EXIT
awk '
  /^[[:space:]]*#/ { next }                  # comment lines
  /^[[:space:]]*$/ { next }                  # blank lines
  /^[A-Za-z_][A-Za-z0-9_]*=/ {
    eq = index($0, "=")
    key = substr($0, 1, eq - 1)
    val = substr($0, eq + 1)
    gsub(/^["\x27]|["\x27]$/, "", val)       # strip surrounding quotes
    gsub(/"/, "\\\"", val)                   # escape inner quotes
    printf "%s: \"%s\"\n", key, val
  }
' "$ENV_FILE" > "$ENV_YAML"
env_yaml() { echo "$ENV_YAML"; }

# ─── one-time project setup ──────────────────────────────────────────────────
bootstrap() {
  gcloud config set project "$PROJECT_ID"
  gcloud config set run/region "$REGION"
  gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com \
    storage.googleapis.com

  log "Storing the service account key in Secret Manager as '${GCP_KEY_SECRET}'"
  if ! gcloud secrets describe "$GCP_KEY_SECRET" >/dev/null 2>&1; then
    gcloud secrets create "$GCP_KEY_SECRET" --replication-policy=automatic
  fi
  gcloud secrets versions add "$GCP_KEY_SECRET" --data-file="$LOCAL_KEY_FILE"

  log "Letting the Cloud Run runtime service account read that secret"
  local project_number
  project_number=$(gcloud projects describe "$PROJECT_ID" \
    --format="value(projectNumber)")
  gcloud secrets add-iam-policy-binding "$GCP_KEY_SECRET" \
    --member="serviceAccount:${project_number}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
}

# ─── API service ─────────────────────────────────────────────────────────────
deploy_api() {
  log "Deploying ${API_SERVICE} from ${API_DIR} (Cloud Build builds the image)"
  gcloud run deploy "$API_SERVICE" \
    --source "$API_DIR" \
    --region "$REGION" \
    --port="$API_PORT" \
    --allow-unauthenticated \
    --timeout=900 \
    --memory=4Gi \
    --max-instances=1 \
    --env-vars-file="$(env_yaml)" \
    --set-secrets="$SECRET_MOUNT_PATH=$GCP_KEY_SECRET:latest"

  # .env points GCP_SERVICE_ACCOUNT_KEY at a path on your laptop.
  gcloud run services update "$API_SERVICE" \
    --region "$REGION" \
    --update-env-vars=GCP_SERVICE_ACCOUNT_KEY="$SECRET_MOUNT_PATH"
}

api_url() {
  local url
  url=$(gcloud run services describe "$API_SERVICE" \
    --region "$REGION" --format="value(urls[0])")
  if [ -z "$url" ]; then
    url=$(gcloud run services describe "$API_SERVICE" \
      --region "$REGION" --format="value(status.url)")
  fi
  if [ -z "$url" ]; then
    echo "ERROR: could not resolve the URL for Cloud Run service" \
         "'$API_SERVICE' in $REGION. Deploy it first." >&2
    exit 1
  fi
  echo "$url"
}

# ─── Web app ─────────────────────────────────────────────────────────────────
deploy_web() {
  local url; url="$(api_url)"

  log "Deploying ${WEB_SERVICE} from ${WEB_DIR}, pointing at ${url}"
  gcloud run deploy "$WEB_SERVICE" \
    --source "$WEB_DIR" \
    --region "$REGION" \
    --port="$WEB_PORT" \
    --allow-unauthenticated \
    --max-instances=1 \
    --session-affinity \
    --memory=2Gi \
    --env-vars-file="$(env_yaml)" \
    --set-secrets="$SECRET_MOUNT_PATH=$GCP_KEY_SECRET:latest"

  # API_SERVICE_URL is only discoverable after the API is deployed.
  gcloud run services update "$WEB_SERVICE" \
    --region "$REGION" \
    --update-env-vars=GCP_SERVICE_ACCOUNT_KEY="$SECRET_MOUNT_PATH",API_SERVICE_URL="$url"
}

# ─── Cloud Scheduler ─────────────────────────────────────────────────────────
deploy_scheduler() {
  local url; url="$(api_url)"

  while IFS='|' read -r job_name endpoint cron body; do
    [ -z "${job_name// /}" ] && continue

    local action=create
    if gcloud scheduler jobs describe "$job_name" \
         --location="$REGION" >/dev/null 2>&1 </dev/null; then
      action=update
    fi

    log "Running '${action}' on scheduler job '${job_name}' (${cron} ${SCHEDULER_TZ})"
    gcloud scheduler jobs "$action" http "$job_name" \
      --location="$REGION" \
      --schedule="$cron" \
      --time-zone="$SCHEDULER_TZ" \
      --uri="${url}${endpoint}" \
      --http-method=POST \
      --headers="Content-Type=application/json" \
      --message-body="$body" \
      --attempt-deadline=1800s \
      </dev/null

    log "Triggering '${job_name}' once so you can confirm it works"
    gcloud scheduler jobs run "$job_name" --location="$REGION" </dev/null
  done <<< "$SCHEDULE_SPECS"

  echo
  echo "Check the results with:"
  echo "  gcloud run services logs read ${API_SERVICE} --region=${REGION} --limit=50"
}

show_urls() {
  echo "API     : $(api_url)"
  echo "API docs: $(api_url)/docs"
  echo "Web app : $(gcloud run services describe "$WEB_SERVICE" \
    --region "$REGION" --format='value(status.url)')"
}

# ─── entry point ─────────────────────────────────────────────────────────────
case "${1:-all}" in
  all)       bootstrap; deploy_api; deploy_web; deploy_scheduler; show_urls ;;
  bootstrap) bootstrap ;;
  api)       deploy_api ;;
  web)       deploy_web ;;
  scheduler) deploy_scheduler ;;
  urls)      show_urls ;;
  *) echo "usage: $0 {all|bootstrap|api|web|scheduler|urls}" >&2; exit 1 ;;
esac

log "Done."
