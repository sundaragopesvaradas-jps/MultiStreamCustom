#!/usr/bin/env bash
# Cancel a pending grace-period complete of YouTube/Facebook lives.
set -uo pipefail

RUN_DIR="/opt/multistream/run"
PID_FILE="${RUN_DIR}/stream-end.pid"
TOKEN_FILE="${RUN_DIR}/stream-end.token"
LOG_FILE="/var/log/multistream/stream-end.log"

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${old_pid:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" 2>/dev/null || true
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cancelled by on-stream-ready (pid=${old_pid})" >>"$LOG_FILE"
  fi
fi
rm -f "$PID_FILE" "$TOKEN_FILE"
echo "cancelled pending platform-live end"
