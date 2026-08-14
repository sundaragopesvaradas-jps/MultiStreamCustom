#!/usr/bin/env bash
# Push a published MediaMTX path to YouTube + Facebook (stream copy, no re-encode).
set -euo pipefail

PATH_NAME="${1:-}"
if [[ -z "$PATH_NAME" ]]; then
  echo "usage: push-destinations.sh <mtx-path>" >&2
  exit 1
fi

ENV_FILE="/opt/multistream/etc/destinations.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE — run sync-secrets.sh first" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

INGEST_KEY="${INGEST_STREAM_KEY:-}"
# Path is live/<ingestKey> — reject wrong keys
EXPECTED="live/${INGEST_KEY}"
if [[ -n "$INGEST_KEY" && "$PATH_NAME" != "$EXPECTED" ]]; then
  echo "rejecting path $PATH_NAME (expected $EXPECTED)" >&2
  exit 1
fi

RUN_DIR="/opt/multistream/run"
mkdir -p "$RUN_DIR"
SAFE_NAME="${PATH_NAME//\//_}"
PID_FILE="${RUN_DIR}/${SAFE_NAME}.pids"

# Local RTMP read from MediaMTX (same process host)
INPUT="rtmp://127.0.0.1:1935/${PATH_NAME}"

stop_existing() {
  if [[ -f "$PID_FILE" ]]; then
    while read -r pid; do
      kill "$pid" 2>/dev/null || true
    done < "$PID_FILE"
    rm -f "$PID_FILE"
  fi
}

stop_existing
: > "$PID_FILE"

start_push() {
  local name="$1"
  local url="$2"
  local log="/var/log/multistream/${SAFE_NAME}-${name}.log"
  mkdir -p /var/log/multistream
  # -c copy: no re-encode (best quality + lowest CPU/latency on this path)
  nohup ffmpeg -hide_banner -loglevel warning -i "$INPUT" \
    -c copy -f flv "$url" >>"$log" 2>&1 &
  echo $! >> "$PID_FILE"
  echo "started $name push pid=$!"
}

if [[ -n "${YOUTUBE_RTMP_URL:-}" && "$YOUTUBE_RTMP_URL" != *REPLACE_ME* ]]; then
  start_push youtube "$YOUTUBE_RTMP_URL"
else
  echo "YouTube key not set — skip"
fi

if [[ -n "${FACEBOOK_RTMP_URL:-}" && "$FACEBOOK_RTMP_URL" != *REPLACE_ME* ]]; then
  start_push facebook "$FACEBOOK_RTMP_URL"
else
  echo "Facebook key not set — skip"
fi

# Keep runOnReady alive while children run (MediaMTX kills the hook process group on not-ready)
if [[ -s "$PID_FILE" ]]; then
  wait || true
fi
