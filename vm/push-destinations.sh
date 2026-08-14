#!/usr/bin/env bash
# Push a published MediaMTX path to YouTube + Facebook (stream copy, no re-encode).
# Records a per-destination result to the session history on exit.
set -uo pipefail

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
EXPECTED="live/${INGEST_KEY}"
if [[ -n "$INGEST_KEY" && "$PATH_NAME" != "$EXPECTED" ]]; then
  echo "rejecting path $PATH_NAME (expected $EXPECTED)" >&2
  exit 1
fi

RUN_DIR="/opt/multistream/run"
LOG_DIR="/var/log/multistream"
mkdir -p "$RUN_DIR" "$LOG_DIR"
chmod 700 "$LOG_DIR"

SAFE_NAME="${PATH_NAME//\//_}"
PID_FILE="${RUN_DIR}/${SAFE_NAME}.pids"
SESSION_ID="$(date -u +%Y%m%dT%H%M%SZ)"
SESSION_FILE="${RUN_DIR}/current-session"

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
printf '%s\n' "$SESSION_ID" > "$SESSION_FILE"

declare -A DEST_PID DEST_LOG DEST_PROGRESS DEST_START

start_push() {
  local name="$1"
  local url="$2"
  local log="${LOG_DIR}/${SESSION_ID}-${name}.log"
  local progress="${RUN_DIR}/${SESSION_ID}-${name}.progress"

  : > "$log"
  chmod 600 "$log"
  : > "$progress"

  # -c copy: no re-encode (best quality, lowest CPU/latency)
  ffmpeg -hide_banner -loglevel info -nostdin \
    -i "$INPUT" \
    -c copy -f flv -flvflags no_duration_filesize \
    -progress "$progress" \
    "$url" >>"$log" 2>&1 &

  local pid=$!
  DEST_PID[$name]=$pid
  DEST_LOG[$name]="$log"
  DEST_PROGRESS[$name]="$progress"
  DEST_START[$name]="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "$pid" >> "$PID_FILE"
  echo "started $name push pid=$pid"
}

finalize() {
  local name pid rc
  for name in "${!DEST_PID[@]}"; do
    pid="${DEST_PID[$name]}"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null
    rc=$?
    /opt/multistream/bin/record-session.py \
      --session "$SESSION_ID" \
      --destination "$name" \
      --path "$PATH_NAME" \
      --started "${DEST_START[$name]}" \
      --exit-code "$rc" \
      --log-file "${DEST_LOG[$name]}" \
      --progress-file "${DEST_PROGRESS[$name]}" || true
    rm -f "${DEST_PROGRESS[$name]}"
  done
  rm -f "$SESSION_FILE"
}

trap 'finalize; exit 0' TERM INT

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

if [[ ${#DEST_PID[@]} -gt 0 ]]; then
  wait
  finalize
fi
