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
rsync -a --delete "$ROOT/recording/" /opt/multistream/recording/
/opt/multistream/ui/.venv/bin/pip install -q -r /opt/multistream/ui/requirements.txt

mkdir -p /var/log/multistream /opt/multistream/run /var/lib/multistream/recordings
chmod 700 /var/log/multistream /opt/multistream/run /var/lib/multistream/recordings

mkdir -p /opt/multistream/zoom-sdk/scripts
if [[ -d "$ROOT/vm/zoom-sdk" ]]; then
  rsync -a "$ROOT/vm/zoom-sdk/" /opt/multistream/zoom-sdk/scripts/
  install -m 755 "$ROOT/vm/zoom-sdk/build-recorder.sh" /opt/multistream/bin/zoom-sdk-build.sh
  install -m 755 "$ROOT/vm/zoom-sdk/setup-audio.sh" /opt/multistream/bin/zoom-sdk-setup-audio.sh
fi
if [[ -x /opt/multistream/zoom-sdk/sample/demo/bin/meetingSDKDemo ]]; then
  install -m 755 "$ROOT/vm/zoom-sdk/zoom-sdk-recorder.sh" /opt/multistream/bin/zoom-sdk-recorder
elif [[ ! -x /opt/multistream/bin/zoom-sdk-recorder ]] \
  || grep -q "not installed yet" /opt/multistream/bin/zoom-sdk-recorder 2>/dev/null; then
  install -m 755 "$ROOT/vm/zoom-sdk-recorder.placeholder.sh" /opt/multistream/bin/zoom-sdk-recorder
fi

echo "==> Recording timers"
cp "$ROOT/vm/systemd/multistream-recording.service" /etc/systemd/system/
cp "$ROOT/vm/systemd/multistream-recording.timer" /etc/systemd/system/
cp "$ROOT/vm/systemd/multistream-recording-purge.service" /etc/systemd/system/
cp "$ROOT/vm/systemd/multistream-recording-purge.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now multistream-recording.timer
systemctl enable --now multistream-recording-purge.timer

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
systemctl is-active multistream-recording.timer multistream-recording-purge.timer || true
echo "=== Update complete ==="
echo "deploy_marker=$REV"
