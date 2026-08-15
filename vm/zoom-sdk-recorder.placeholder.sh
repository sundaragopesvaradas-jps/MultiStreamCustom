#!/usr/bin/env bash
# Placeholder wrapper until the Zoom Linux Meeting SDK raw-data binary is installed.
# Replace this file with the real SDK sample binary (same path) after following
# docs/RECORDING_SETUP.md.
set -euo pipefail
echo "zoom-sdk-recorder: Meeting SDK binary not installed yet." >&2
echo "Install the Zoom Linux Meeting SDK raw-data sample as /opt/multistream/bin/zoom-sdk-recorder" >&2
echo "Job file: ${1:-} ${2:-}" >&2
exit 1
