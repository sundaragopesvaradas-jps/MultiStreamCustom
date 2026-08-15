#!/usr/bin/env bash
# Launch the Zoom Meeting SDK raw-data demo from a Multistream job JSON.
#
# Usage: zoom-sdk-recorder --job /path/to/job.json
#
# Job fields: meeting_number, token, meeting_password, recording_token,
# display_name, output_path, sdk_key (optional).
set -euo pipefail

SDK_ROOT="${ZOOM_SDK_ROOT:-/opt/multistream/zoom-sdk}"
DEMO_BIN="${ZOOM_SDK_DEMO_BIN:-$SDK_ROOT/sample/demo/bin/meetingSDKDemo}"
DEMO_DIR="$(cd "$(dirname "$DEMO_BIN")" && pwd)"
LIB_DIR="${ZOOM_SDK_LIB_DIR:-$SDK_ROOT/sample/demo/lib/zoom_meeting_sdk}"
SETUP_AUDIO="${ZOOM_SDK_SETUP_AUDIO:-/opt/multistream/bin/zoom-sdk-setup-audio.sh}"
WORK_ROOT="${ZOOM_SDK_WORK_ROOT:-/opt/multistream/run/recording-work}"
LOG="${ZOOM_SDK_RECORDER_LOG:-/var/log/multistream/zoom-sdk-recorder.log}"
CONFIG_LOCK="${ZOOM_SDK_CONFIG_LOCK:-/opt/multistream/run/zoom-sdk-config.lock}"

job_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --job)
      job_path="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$job_path" || ! -f "$job_path" ]]; then
  echo "Usage: $0 --job /path/to/job.json" >&2
  exit 2
fi

if [[ ! -x "$DEMO_BIN" ]]; then
  echo "Meeting SDK demo binary missing: $DEMO_BIN" >&2
  echo "Run /opt/multistream/bin/zoom-sdk-build.sh first." >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG")" "$WORK_ROOT"
exec >>"$LOG" 2>&1

echo "==== $(date -u +%Y-%m-%dT%H:%M:%SZ) zoom-sdk-recorder start ===="
echo "job=$job_path"

# shellcheck disable=SC2016
eval "$(python3 - "$job_path" <<'PY'
import json, shlex, sys
job = json.load(open(sys.argv[1], encoding="utf-8"))
def q(key, default=""):
    print(f"{key}={shlex.quote(str(job.get(key, default) or ''))}")
q("meeting_number")
q("token")
q("meeting_password")
q("recording_token")
q("display_name", "ISKCON Deoghar Archive")
q("output_path")
PY
)"

if [[ -z "$meeting_number" || -z "$token" || -z "$output_path" ]]; then
  echo "job JSON missing meeting_number, token, or output_path" >&2
  exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="$WORK_ROOT/$stamp-$$"
mkdir -p "$work" "$(dirname "$CONFIG_LOCK")"
cd "$work"

# The Zoom sample always loads config.txt from the binary directory, not cwd.
# Serialize writers so two overlapping jobs cannot clobber each other.
exec 9>"$CONFIG_LOCK"
if ! flock -n 9; then
  echo "Another zoom-sdk-recorder holds $CONFIG_LOCK" >&2
  exit 1
fi

cat > "$DEMO_DIR/config.txt" <<EOF
meeting_number: "$meeting_number"
token: "$token"
meeting_password: "$meeting_password"
recording_token: "$recording_token"
display_name: "$display_name"
GetVideoRawData: "true"
GetAudioRawData: "true"
SendVideoRawData: "false"
SendAudioRawData: "false"
EOF
chmod 600 "$DEMO_DIR/config.txt"
# Keep a copy in the workdir for debugging.
cp -f "$DEMO_DIR/config.txt" "$work/config.txt"

if [[ -x "$SETUP_AUDIO" ]]; then
  "$SETUP_AUDIO" || echo "WARNING: PulseAudio setup failed; audio may be empty"
fi

export HOME="${HOME:-/root}"
export LD_LIBRARY_PATH="$LIB_DIR:${LD_LIBRARY_PATH:-}"

demo_pid=""
cleanup() {
  local code=$?
  if [[ -n "${demo_pid:-}" ]] && kill -0 "$demo_pid" 2>/dev/null; then
    echo "Stopping demo pid=$demo_pid"
    kill -TERM "$demo_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$demo_pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$demo_pid" 2>/dev/null || true
    wait "$demo_pid" 2>/dev/null || true
  fi

  mkdir -p "$(dirname "$output_path")"
  local width=1280 height=720
  if [[ -f video.size ]]; then
    # shellcheck disable=SC1090
    source video.size || true
  fi

  if [[ -s output.yuv && -s audio.pcm ]]; then
    echo "Muxing YUV+PCM -> $output_path (${width}x${height})"
    ffmpeg -y -hide_banner -loglevel error \
      -f s16le -ar 32000 -ac 1 -i audio.pcm \
      -f rawvideo -pix_fmt yuv420p -s "${width}x${height}" -r 25 -i output.yuv \
      -c:v libx264 -preset veryfast -crf 23 \
      -c:a aac -b:a 128k -shortest \
      "$output_path" || code=1
  elif [[ -s audio.pcm ]]; then
    echo "Muxing audio-only -> $output_path"
    ffmpeg -y -hide_banner -loglevel error \
      -f s16le -ar 32000 -ac 1 -i audio.pcm \
      -c:a aac -b:a 128k "$output_path" || code=1
  elif [[ -s output.yuv ]]; then
    echo "Muxing video-only -> $output_path (${width}x${height})"
    ffmpeg -y -hide_banner -loglevel error \
      -f rawvideo -pix_fmt yuv420p -s "${width}x${height}" -r 25 -i output.yuv \
      -c:v libx264 -preset veryfast -crf 23 "$output_path" || code=1
  else
    echo "No raw media produced in $work" >&2
    code=1
  fi

  if [[ -f "$output_path" ]]; then
    ls -lh "$output_path"
  fi
  # Keep workdir on failure for debugging; wipe on success.
  if [[ $code -eq 0 ]]; then
    rm -rf "$work"
  else
    echo "Kept workdir: $work"
  fi
  echo "==== $(date -u +%Y-%m-%dT%H:%M:%SZ) zoom-sdk-recorder end code=$code ===="
  exit "$code"
}
trap cleanup EXIT INT TERM

"$DEMO_BIN" &
demo_pid=$!
echo "demo started pid=$demo_pid work=$work"
wait "$demo_pid" || true
demo_pid=""
