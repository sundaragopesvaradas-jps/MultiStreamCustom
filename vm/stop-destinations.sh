#!/usr/bin/env bash
# Stop FFmpeg push processes for a MediaMTX path.
set -euo pipefail

PATH_NAME="${1:-}"
SAFE_NAME="${PATH_NAME//\//_}"
PID_FILE="/opt/multistream/run/${SAFE_NAME}.pids"

if [[ -f "$PID_FILE" ]]; then
  while read -r pid; do
    kill "$pid" 2>/dev/null || true
  done < "$PID_FILE"
  rm -f "$PID_FILE"
  echo "stopped pushes for $PATH_NAME"
fi
