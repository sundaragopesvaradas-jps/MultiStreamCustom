#!/usr/bin/env bash
# After Zoom goes idle: wait a grace period, then complete YT/FB lives unless
# Zoom comes back (on-stream-ready cancels this waiter).
set -uo pipefail

RUN_DIR="/opt/multistream/run"
mkdir -p "$RUN_DIR"

TOKEN_FILE="${RUN_DIR}/stream-end.token"
PID_FILE="${RUN_DIR}/stream-end.pid"
LOG_FILE="/var/log/multistream/stream-end.log"

# Cancel any previous waiter.
if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${old_pid:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

TOKEN="$(date -u +%Y%m%dT%H%M%S%NZ)-$$"
printf '%s\n' "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

GRACE="$(
  /opt/multistream/ui/.venv/bin/python - <<'PY' 2>/dev/null || echo 30
import os, sys
from pathlib import Path
sys.path.insert(0, "/opt/multistream/ui")
import keyvault, platforms
env = {}
p = Path("/opt/multistream/etc/multistream.env")
if p.exists():
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("'").strip('"')
vault = os.environ.get("KEY_VAULT_NAME") or env.get("KEY_VAULT_NAME", "")
if not vault:
    print(30)
    raise SystemExit
get = lambda n: keyvault.get_secret(vault, n)
print(platforms.stream_end_grace_seconds(get))
PY
)"
if ! [[ "$GRACE" =~ ^[0-9]+$ ]]; then
  GRACE=30
fi
if (( GRACE < 5 )); then GRACE=5; fi
if (( GRACE > 600 )); then GRACE=600; fi

# Detach so MediaMTX ending runOnNotReady cannot kill the waiter.
nohup bash -c '
set -uo pipefail
TOKEN="$1"
GRACE="$2"
TOKEN_FILE="$3"
PID_FILE="$4"
LOG_FILE="$5"

zoom_still_idle() {
  ! curl -fsS "http://127.0.0.1:9997/v3/paths/list" 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(i.get(\"ready\") for i in d.get(\"items\",[])) else 1)"
}

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) grace=${GRACE}s token=${TOKEN} — waiting" >>"$LOG_FILE"
sleep "$GRACE"
if [[ ! -f "$TOKEN_FILE" ]] || [[ "$(cat "$TOKEN_FILE" 2>/dev/null)" != "$TOKEN" ]]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cancelled (token mismatch)" >>"$LOG_FILE"
  exit 0
fi
if ! zoom_still_idle; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cancelled (Zoom publishing again)" >>"$LOG_FILE"
  rm -f "$TOKEN_FILE" "$PID_FILE"
  exit 0
fi
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) completing platform lives" >>"$LOG_FILE"
/opt/multistream/ui/.venv/bin/python /opt/multistream/bin/end-platform-lives.py >>"$LOG_FILE" 2>&1 || true
rm -f "$TOKEN_FILE" "$PID_FILE"
' _ "$TOKEN" "$GRACE" "$TOKEN_FILE" "$PID_FILE" "$LOG_FILE" >/dev/null 2>&1 &

echo $! > "$PID_FILE"
chmod 600 "$PID_FILE"
echo "scheduled platform-live end in ${GRACE}s (pid=$(cat "$PID_FILE"))"
