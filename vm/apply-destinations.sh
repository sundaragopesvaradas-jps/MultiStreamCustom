#!/usr/bin/env bash
# Apply enabled destination toggles: start/stop YouTube and Facebook pushes
# independently so they can flip mid-stream.
set -uo pipefail

ENV_FILE="/opt/multistream/etc/destinations.env"
ENABLED_FILE="/opt/multistream/etc/enabled.env"
RUN_DIR="/opt/multistream/run"
LOG_DIR="/var/log/multistream"
PATH_FILE="${RUN_DIR}/current-path"
SESSION_FILE="${RUN_DIR}/current-session"

if [[ ! -f "$ENABLED_FILE" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    cat > "$ENABLED_FILE" <<'EOF'
YOUTUBE_ENABLED=1
FACEBOOK_ENABLED=1
EOF
    chmod 600 "$ENABLED_FILE"
  else
    YOUTUBE_ENABLED=1
    FACEBOOK_ENABLED=1
  fi
fi

if [[ -f "$ENABLED_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENABLED_FILE"
fi
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

mkdir -p "$RUN_DIR" "$LOG_DIR" 2>/dev/null || true
if [[ "$(id -u)" -eq 0 ]]; then
  chmod 700 "$LOG_DIR" 2>/dev/null || true
fi
is_enabled() {
  local name="$1"
  case "$name" in
    youtube) [[ "${YOUTUBE_ENABLED:-1}" == "1" ]] ;;
    facebook) [[ "${FACEBOOK_ENABLED:-1}" == "1" ]] ;;
    *) return 1 ;;
  esac
}

dest_url() {
  case "$1" in
    youtube) echo "${YOUTUBE_RTMP_URL:-}" ;;
    facebook) echo "${FACEBOOK_RTMP_URL:-}" ;;
  esac
}

pid_file() { echo "${RUN_DIR}/$1.pid"; }
meta_file() { echo "${RUN_DIR}/$1.meta"; }
progress_file() { echo "${RUN_DIR}/$1.progress"; }

is_running() {
  local pf
  pf="$(pid_file "$1")"
  [[ -f "$pf" ]] || return 1
  local pid
  pid="$(cat "$pf")"
  kill -0 "$pid" 2>/dev/null
}

stream_live() {
  [[ -f "$PATH_FILE" ]] || return 1
  local path
  path="$(cat "$PATH_FILE")"
  [[ -n "$path" ]] || return 1
  curl -fsS "http://127.0.0.1:9997/v3/paths/list" 2>/dev/null \
    | python3 -c "import json,sys; p=sys.argv[1]; d=json.load(sys.stdin); sys.exit(0 if any(i.get('name')==p and i.get('ready') for i in d.get('items',[])) else 1)" "$path"
}

ensure_session() {
  if [[ ! -f "$SESSION_FILE" ]]; then
    date -u +%Y%m%dT%H%M%SZ > "$SESSION_FILE"
  fi
  cat "$SESSION_FILE"
}

start_one() {
  local name="$1"
  local url path session log progress meta

  if is_running "$name"; then
    echo "$name already running"
    return 0
  fi

  if ! stream_live; then
    echo "$name: no live Zoom stream — will start when Zoom goes live"
    return 0
  fi

  url="$(dest_url "$name")"
  if [[ -z "$url" || "$url" == *REPLACE_ME* ]]; then
    echo "$name: stream key not set — skip"
    return 0
  fi

  path="$(cat "$PATH_FILE")"
  session="$(ensure_session)"
  log="${LOG_DIR}/${session}-${name}.log"
  progress="$(progress_file "$name")"
  meta="$(meta_file "$name")"

  : > "$log"
  chmod 600 "$log"
  : > "$progress"

  ffmpeg -hide_banner -loglevel info -nostdin \
    -i "rtmp://127.0.0.1:1935/${path}" \
    -c copy -f flv -flvflags no_duration_filesize \
    -progress "$progress" \
    "$url" >>"$log" 2>&1 &

  local pid=$!
  echo "$pid" > "$(pid_file "$name")"
  cat > "$meta" <<EOF
session=${session}
destination=${name}
path=${path}
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
log=${log}
progress=${progress}
EOF
  chmod 600 "$meta" "$(pid_file "$name")"
  echo "started $name pid=$pid"
}

record_and_cleanup() {
  local name="$1"
  local rc="${2:-0}"
  local meta session path started log progress

  meta="$(meta_file "$name")"
  [[ -f "$meta" ]] || return 0

  # shellcheck disable=SC1090
  source "$meta"
  session="${session:-unknown}"
  path="${path:-}"
  started="${started:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
  log="${log:-}"
  progress="${progress:-}"

  if [[ -n "$log" ]]; then
    /opt/multistream/bin/record-session.py \
      --session "$session" \
      --destination "$name" \
      --path "$path" \
      --started "$started" \
      --exit-code "$rc" \
      --log-file "$log" \
      --progress-file "${progress:-}" || true
  fi

  rm -f "$(pid_file "$name")" "$meta" "$(progress_file "$name")"
}

stop_one() {
  local name="$1"
  local pf pid rc=0

  pf="$(pid_file "$name")"
  if [[ ! -f "$pf" ]]; then
    echo "$name not running"
    return 0
  fi

  pid="$(cat "$pf")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null
    rc=$?
  else
    rc=1
  fi

  record_and_cleanup "$name" "$rc"
  echo "stopped $name"
}

# Reap crashed processes so history is still written
reap_dead() {
  local name
  for name in youtube facebook; do
    if [[ -f "$(pid_file "$name")" ]] && ! is_running "$name"; then
      echo "$name process died — recording"
      record_and_cleanup "$name" 1
    fi
  done
}

apply_all() {
  local name
  reap_dead
  for name in youtube facebook; do
    if is_enabled "$name"; then
      start_one "$name"
    else
      stop_one "$name"
    fi
  done
}

stop_all() {
  local name
  for name in youtube facebook; do
    stop_one "$name"
  done
  rm -f "$PATH_FILE" "$SESSION_FILE"
}

case "${1:-apply}" in
  apply) apply_all ;;
  stop-all) stop_all ;;
  start)
    shift
    start_one "${1:?destination required}"
    ;;
  stop)
    shift
    stop_one "${1:?destination required}"
    ;;
  status)
    for name in youtube facebook; do
      if is_running "$name"; then
        echo "$name=running"
      else
        echo "$name=stopped"
      fi
    done
    if stream_live; then
      echo "zoom=live path=$(cat "$PATH_FILE")"
    else
      echo "zoom=idle"
    fi
    ;;
  *)
    echo "usage: $0 apply|stop-all|start <dest>|stop <dest>|status" >&2
    exit 1
    ;;
esac
