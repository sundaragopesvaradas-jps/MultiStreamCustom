#!/usr/bin/env bash
# Redeploy MultiStream app code and relay scripts on an installed VM.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Relay scripts"
install -m 755 "$ROOT/vm/apply-destinations.sh" /opt/multistream/bin/apply-destinations.sh
install -m 755 "$ROOT/vm/on-stream-ready.sh" /opt/multistream/bin/on-stream-ready.sh
install -m 755 "$ROOT/vm/on-stream-end.sh" /opt/multistream/bin/on-stream-end.sh
install -m 755 "$ROOT/vm/push-destinations.sh" /opt/multistream/bin/push-destinations.sh
install -m 755 "$ROOT/vm/stop-destinations.sh" /opt/multistream/bin/stop-destinations.sh
install -m 755 "$ROOT/vm/sync-secrets.sh" /opt/multistream/bin/sync-secrets.sh
install -m 755 "$ROOT/vm/refresh-ui-env.sh" /opt/multistream/bin/refresh-ui-env.sh
install -m 755 "$ROOT/vm/record-session.py" /opt/multistream/bin/record-session.py
install -m 755 "$ROOT/vm/auto-prepare-live.py" /opt/multistream/bin/auto-prepare-live.py
install -m 644 "$ROOT/vm/mediamtx.yml" /opt/multistream/etc/mediamtx.yml
install -m 644 "$ROOT/vm/logrotate-multistream" /etc/logrotate.d/multistream

if [[ ! -f /opt/multistream/etc/enabled.env ]]; then
  install -m 644 "$ROOT/vm/enabled.env" /opt/multistream/etc/enabled.env
fi

echo "==> UI"
rsync -a --delete --exclude '.venv' "$ROOT/ui/" /opt/multistream/ui/
/opt/multistream/ui/.venv/bin/pip install -q -r /opt/multistream/ui/requirements.txt

mkdir -p /var/log/multistream /opt/multistream/run
chmod 700 /var/log/multistream /opt/multistream/run

# Remove any leftover Meeting SDK recording install from earlier releases.
systemctl disable --now multistream-recording.timer multistream-recording-purge.timer 2>/dev/null || true
rm -f /etc/systemd/system/multistream-recording.service \
  /etc/systemd/system/multistream-recording.timer \
  /etc/systemd/system/multistream-recording-purge.service \
  /etc/systemd/system/multistream-recording-purge.timer
rm -rf /opt/multistream/recording /opt/multistream/zoom-sdk \
  /var/lib/multistream/recordings /opt/multistream/run/recording-work \
  /opt/multistream/run/recording-jobs
rm -f /opt/multistream/bin/zoom-sdk-recorder \
  /opt/multistream/bin/zoom-sdk-build.sh \
  /opt/multistream/bin/zoom-sdk-setup-audio.sh \
  /opt/multistream/bin/zoom-sdk-selftest.sh \
  /opt/multistream/etc/recording-schedule.json \
  /opt/multistream/run/zoom-sdk-recorder.json \
  /opt/multistream/run/recording-processing.json \
  /opt/multistream/run/recording-alert-state.json \
  /var/log/multistream/zoom-sdk-recorder.log
pkill -f meetingSDKDemo 2>/dev/null || true
pkill -f zoom-sdk-recorder 2>/dev/null || true

echo "==> Restart"
/opt/multistream/bin/refresh-ui-env.sh
REV="${DEPLOY_REVISION:-}"
if [[ -z "$REV" ]]; then
  REV="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
fi
printf '%s\n' "$REV" > /opt/multistream/etc/deploy-revision
date -u +%Y-%m-%dT%H:%M:%SZ > /opt/multistream/etc/deployed-at
systemctl restart multistream-ui.service
systemctl restart mediamtx.service

sleep 2
systemctl is-active multistream-ui.service mediamtx.service
echo "=== Update complete ==="
echo "deploy_marker=$REV"
