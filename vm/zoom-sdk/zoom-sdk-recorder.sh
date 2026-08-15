#!/usr/bin/env bash
# Launch the Zoom Meeting SDK raw-data demo from a Multistream job JSON and
# encode its output live.
#
# Usage: zoom-sdk-recorder --job /path/to/job.json
#
# Job fields: meeting_number, token, meeting_password, recording_token,
# display_name, output_path, sdk_key (optional).
#
# The demo writes I420 frames and PCM into two FIFOs and a single ffmpeg reads
# both, so nothing raw ever lands on disk and the MP4 is finished within a
# second of the bot leaving. This script holds a read/write handle on each FIFO
# for the whole run: that stops the demo blocking before ffmpeg attaches, and it
# means closing those handles at the end is what signals end-of-stream.
set -euo pipefail

SDK_ROOT="${ZOOM_SDK_ROOT:-/opt/multistream/zoom-sdk}"
DEMO_BIN="${ZOOM_SDK_DEMO_BIN:-$SDK_ROOT/sample/demo/bin/meetingSDKDemo}"
DEMO_DIR="$(cd "$(dirname "$DEMO_BIN")" && pwd)"
LIB_DIR="${ZOOM_SDK_LIB_DIR:-$SDK_ROOT/sample/demo/lib/zoom_meeting_sdk}"
SETUP_AUDIO="${ZOOM_SDK_SETUP_AUDIO:-/opt/multistream/bin/zoom-sdk-setup-audio.sh}"
WORK_ROOT="${ZOOM_SDK_WORK_ROOT:-/opt/multistream/run/recording-work}"
LOG="${ZOOM_SDK_RECORDER_LOG:-/var/log/multistream/zoom-sdk-recorder.log}"
CONFIG_LOCK="${ZOOM_SDK_CONFIG_LOCK:-/opt/multistream/run/zoom-sdk-config.lock}"

VIDEO_WIDTH="${MULTISTREAM_VIDEO_WIDTH:-1280}"
VIDEO_HEIGHT="${MULTISTREAM_VIDEO_HEIGHT:-720}"
VIDEO_FPS="${MULTISTREAM_VIDEO_FPS:-15}"
AUDIO_RATE="${MULTISTREAM_AUDIO_RATE:-32000}"
X264_PRESET="${MULTISTREAM_X264_PRESET:-veryfast}"
X264_CRF="${MULTISTREAM_X264_CRF:-26}"
# Budget for ffmpeg to drain its buffers and rewrite the moov atom.
FINALIZE_TIMEOUT="${MULTISTREAM_FINALIZE_TIMEOUT:-600}"
# How long to give the bot to join and start receiving audio before giving up.
AUDIO_WAIT="${MULTISTREAM_AUDIO_WAIT:-180}"

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
mkdir -p "$work" "$(dirname "$CONFIG_LOCK")" "$(dirname "$output_path")"
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

if [[ -x "$SETUP_AUDIO" ]]; then
  "$SETUP_AUDIO" || echo "WARNING: PulseAudio setup failed; audio may be empty"
fi

audio_fifo="$work/audio.fifo"
video_fifo="$work/video.fifo"
mkfifo -m 600 "$audio_fifo" "$video_fifo"

# Holding both ends open keeps the demo from blocking on open() and keeps ffmpeg
# from seeing end-of-stream while the meeting is still running. Every child is
# started with 7>&- 8>&- so it cannot inherit these handles — an encoder holding
# the write end of its own input would never reach end-of-stream.
exec 7<>"$audio_fifo"
exec 8<>"$video_fifo"

export HOME="${HOME:-/root}"
export LD_LIBRARY_PATH="$LIB_DIR:${LD_LIBRARY_PATH:-}"
audio_ready="$work/audio.started"
export MULTISTREAM_AUDIO_READY="$audio_ready"
export MULTISTREAM_AUDIO_OUT="$audio_fifo"
export MULTISTREAM_VIDEO_OUT="$video_fifo"
export MULTISTREAM_VIDEO_WIDTH="$VIDEO_WIDTH"
export MULTISTREAM_VIDEO_HEIGHT="$VIDEO_HEIGHT"
export MULTISTREAM_VIDEO_FPS="$VIDEO_FPS"

demo_pid=""
ffmpeg_pid=""

cleanup() {
  local code=$?
  trap - EXIT INT TERM

  if [[ -n "$demo_pid" ]] && kill -0 "$demo_pid" 2>/dev/null; then
    echo "Stopping demo pid=$demo_pid"
    kill -TERM "$demo_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$demo_pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$demo_pid" 2>/dev/null || true
  fi

  # Dropping our handles is what gives ffmpeg end-of-stream.
  exec 7>&-
  exec 8>&-

  if [[ -n "$ffmpeg_pid" ]]; then
    # With no audio the encoder never got past probing its first input, so
    # there is nothing to drain and no reason to wait out the full budget.
    local budget="$FINALIZE_TIMEOUT"
    if [[ ! -f "$audio_ready" ]]; then
      echo "No audio was ever captured; not waiting for the encoder" >&2
      budget=15
    fi
    echo "Waiting up to ${budget}s for ffmpeg to finish $output_path"
    local waited=0
    while kill -0 "$ffmpeg_pid" 2>/dev/null; do
      if (( waited >= budget )); then
        echo "ffmpeg still running after ${budget}s; terminating" >&2
        kill -TERM "$ffmpeg_pid" 2>/dev/null || true
        sleep 5
        kill -KILL "$ffmpeg_pid" 2>/dev/null || true
        break
      fi
      sleep 1
      waited=$((waited + 1))
    done
    echo "ffmpeg finished after ${waited}s"
  fi

  if [[ -s "$output_path" ]]; then
    ls -lh "$output_path"
    ffprobe -v error -show_entries format=duration,size -show_entries stream=codec_type,width,height,avg_frame_rate \
      -of default=noprint_wrappers=1 "$output_path" 2>&1 || true
    code=0
  else
    echo "No media produced at $output_path" >&2
    code=1
  fi

  rm -rf "$work"
  echo "==== $(date -u +%Y-%m-%dT%H:%M:%SZ) zoom-sdk-recorder end code=$code ===="
  exit "$code"
}
trap cleanup EXIT INT TERM

# Audio is the master clock: constant 32 kHz PCM. The video FIFO carries a
# constant-rate stream produced by the demo's pacing thread, so neither input
# needs timestamps.
ffmpeg -y -hide_banner -loglevel warning -nostdin \
  -thread_queue_size 1024 -f s16le -ar "$AUDIO_RATE" -ac 1 -i "$audio_fifo" \
  -thread_queue_size 1024 -f rawvideo -pix_fmt yuv420p \
  -video_size "${VIDEO_WIDTH}x${VIDEO_HEIGHT}" -framerate "$VIDEO_FPS" -i "$video_fifo" \
  -map 1:v:0 -map 0:a:0 \
  -c:v libx264 -preset "$X264_PRESET" -crf "$X264_CRF" -pix_fmt yuv420p \
  -g "$((VIDEO_FPS * 4))" \
  -c:a aac -b:a 96k \
  -movflags +faststart \
  "$output_path" 7>&- 8>&- 9>&- &
ffmpeg_pid=$!
echo "ffmpeg started pid=$ffmpeg_pid -> $output_path (${VIDEO_WIDTH}x${VIDEO_HEIGHT}@${VIDEO_FPS})"

sleep 2
if ! kill -0 "$ffmpeg_pid" 2>/dev/null; then
  echo "ffmpeg exited immediately; check arguments above" >&2
  ffmpeg_pid=""
  exit 1
fi

"$DEMO_BIN" 7>&- 8>&- 9>&- &
demo_pid=$!
echo "demo started pid=$demo_pid work=$work"

# Stop as soon as either side goes away, so a dead encoder cannot leave the bot
# sitting in the meeting recording nothing. Audio never starting means the bot
# failed to join VoIP, which is equally pointless to sit through.
waited=0
while kill -0 "$demo_pid" 2>/dev/null; do
  if ! kill -0 "$ffmpeg_pid" 2>/dev/null; then
    echo "ffmpeg exited while the demo was running" >&2
    break
  fi
  if [[ ! -f "$audio_ready" ]] && (( waited >= AUDIO_WAIT )); then
    echo "No audio after ${AUDIO_WAIT}s; the bot never joined VoIP" >&2
    break
  fi
  sleep 2
  waited=$((waited + 2))
done
