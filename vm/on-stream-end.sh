#!/usr/bin/env bash
# MediaMTX runOnNotReady: Zoom stopped publishing.
set -uo pipefail

PATH_NAME="${1:-}"
echo "stream ended: ${PATH_NAME:-unknown}"
/opt/multistream/bin/apply-destinations.sh stop-all
