#!/usr/bin/env bash
# MediaMTX runOnReady: Zoom started publishing.
set -uo pipefail

PATH_NAME="${1:-}"
if [[ -z "$PATH_NAME" ]]; then
  echo "usage: on-stream-ready.sh <mtx-path>" >&2
  exit 1
fi

ENV_FILE="/opt/multistream/etc/destinations.env"
RUN_DIR="/opt/multistream/run"
mkdir -p "$RUN_DIR"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

INGEST_KEY="${INGEST_STREAM_KEY:-}"
EXPECTED="live/${INGEST_KEY}"
if [[ -n "$INGEST_KEY" && "$PATH_NAME" != "$EXPECTED" ]]; then
  echo "rejecting path $PATH_NAME (expected $EXPECTED)" >&2
  exit 1
fi

printf '%s\n' "$PATH_NAME" > "${RUN_DIR}/current-path"
date -u +%Y%m%dT%H%M%SZ > "${RUN_DIR}/current-session"
chmod 600 "${RUN_DIR}/current-path" "${RUN_DIR}/current-session"

/opt/multistream/bin/apply-destinations.sh apply

# Stay alive while Zoom publishes. Mid-stream toggles call apply separately.
while [[ -f "${RUN_DIR}/current-path" ]]; do
  if ! curl -fsS "http://127.0.0.1:9997/v3/paths/list" 2>/dev/null \
      | python3 -c "import json,sys; p=sys.argv[1]; d=json.load(sys.stdin); sys.exit(0 if any(i.get('name')==p and i.get('ready') for i in d.get('items',[])) else 1)" "$PATH_NAME"; then
    break
  fi
  sleep 2
done

exit 0
