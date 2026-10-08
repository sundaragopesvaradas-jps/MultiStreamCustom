#!/usr/bin/env bash
# MediaMTX runOnNotReady: Zoom stopped publishing.
set -uo pipefail

PATH_NAME="${1:-}"
echo "stream ended: ${PATH_NAME:-unknown}"

# Stop YT/FB pushes immediately (same as before).
/opt/multistream/bin/apply-destinations.sh stop-all

# After a configurable grace period, complete YouTube and end Facebook —
# unless Zoom starts publishing again (on-stream-ready cancels the waiter).
/opt/multistream/bin/schedule-stream-end.sh
