#!/usr/bin/env bash
# Exercise the live-encode path in zoom-sdk-recorder without a Zoom meeting.
#
# Feeds real-time synthetic audio and video into the same FIFO arrangement the
# recorder uses, then checks that the MP4 has both tracks and that their
# durations match the wall-clock run time. This is what catches broken ffmpeg
# arguments or FIFO handling before a scheduled recording depends on it.
#
# Usage: selftest-encoder.sh [seconds]
set -euo pipefail

SECONDS_TO_RUN="${1:-30}"
VIDEO_WIDTH="${MULTISTREAM_VIDEO_WIDTH:-1280}"
VIDEO_HEIGHT="${MULTISTREAM_VIDEO_HEIGHT:-720}"
VIDEO_FPS="${MULTISTREAM_VIDEO_FPS:-15}"
AUDIO_RATE="${MULTISTREAM_AUDIO_RATE:-32000}"

work="$(mktemp -d)"
out="$work/selftest.mp4"
audio_fifo="$work/audio.fifo"
video_fifo="$work/video.fifo"
mkfifo -m 600 "$audio_fifo" "$video_fifo"

# Children run with 7>&- 8>&- so the encoder cannot hold its own inputs open.
exec 7<>"$audio_fifo"
exec 8<>"$video_fifo"

cleanup() {
  kill "${feed_a:-}" "${feed_v:-}" "${enc:-}" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

echo "Encoding ${SECONDS_TO_RUN}s of ${VIDEO_WIDTH}x${VIDEO_HEIGHT}@${VIDEO_FPS} ..."
ffmpeg -y -hide_banner -loglevel warning -nostdin \
  -thread_queue_size 1024 -f s16le -ar "$AUDIO_RATE" -ac 1 -i "$audio_fifo" \
  -thread_queue_size 1024 -f rawvideo -pix_fmt yuv420p \
  -video_size "${VIDEO_WIDTH}x${VIDEO_HEIGHT}" -framerate "$VIDEO_FPS" -i "$video_fifo" \
  -map 1:v:0 -map 0:a:0 \
  -c:v libx264 -preset "${MULTISTREAM_X264_PRESET:-veryfast}" -crf "${MULTISTREAM_X264_CRF:-26}" \
  -pix_fmt yuv420p -g "$((VIDEO_FPS * 4))" \
  -c:a aac -b:a 96k -movflags +faststart \
  "$out" 7>&- 8>&- &
enc=$!

# -re paces both feeds at wall-clock speed, like the SDK callbacks do.
ffmpeg -hide_banner -loglevel error -nostdin -re \
  -f lavfi -i "sine=frequency=440:sample_rate=$AUDIO_RATE" \
  -ac 1 -f s16le -y "$audio_fifo" 7>&- 8>&- &
feed_a=$!
ffmpeg -hide_banner -loglevel error -nostdin -re \
  -f lavfi -i "testsrc2=size=${VIDEO_WIDTH}x${VIDEO_HEIGHT}:rate=$VIDEO_FPS" \
  -pix_fmt yuv420p -f rawvideo -y "$video_fifo" 7>&- 8>&- &
feed_v=$!

start="$(date +%s)"
sleep "$SECONDS_TO_RUN"
kill "$feed_a" "$feed_v" 2>/dev/null || true
wait "$feed_a" 2>/dev/null || true
wait "$feed_v" 2>/dev/null || true
exec 7>&-
exec 8>&-
for _ in $(seq 1 60); do
  kill -0 "$enc" 2>/dev/null || break
  sleep 1
done
if kill -0 "$enc" 2>/dev/null; then
  echo "SELFTEST FAILED: encoder did not exit after end-of-stream" >&2
  kill -9 "$enc" 2>/dev/null || true
  exit 1
fi
elapsed=$(( $(date +%s) - start ))

echo "--- ffprobe ---"
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height,avg_frame_rate,duration \
  -show_entries format=duration,size -of default=noprint_wrappers=1 "$out"

python3 - "$out" "$elapsed" <<'PY'
import json
import subprocess
import sys

path, elapsed = sys.argv[1], float(sys.argv[2])
probe = json.loads(subprocess.check_output([
    "ffprobe", "-v", "error", "-print_format", "json",
    "-show_streams", "-show_format", path,
]))
streams = {s["codec_type"]: s for s in probe["streams"]}
problems = []
if "video" not in streams:
    problems.append("no video track")
if "audio" not in streams:
    problems.append("no audio track")

durations = {}
for kind, stream in streams.items():
    value = stream.get("duration") or probe["format"].get("duration")
    durations[kind] = float(value)
    if abs(durations[kind] - elapsed) > 3.0:
        problems.append(f"{kind} is {durations[kind]:.1f}s, expected about {elapsed:.0f}s")

if "video" in durations and "audio" in durations:
    skew = abs(durations["video"] - durations["audio"])
    print(f"track skew: {skew:.2f}s")
    if skew > 1.0:
        problems.append(f"audio and video differ by {skew:.2f}s")

size = int(probe["format"]["size"])
print(f"size: {size / 1024 / 1024:.1f} MiB over {elapsed:.0f}s "
      f"({size * 8 / max(elapsed, 1) / 1000:.0f} kbps)")

if problems:
    print("SELFTEST FAILED: " + "; ".join(problems))
    sys.exit(1)
print("SELFTEST PASSED")
PY
