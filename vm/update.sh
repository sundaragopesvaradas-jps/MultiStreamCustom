#!/usr/bin/env bash
# Redeploy MultiStream app code and relay scripts on an installed VM.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Relay scripts"
install -m 755 "$ROOT/vm/push-destinations.sh" /opt/multistream/bin/push-destinations.sh
install -m 755 "$ROOT/vm/stop-destinations.sh" /opt/multistream/bin/stop-destinations.sh
install -m 755 "$ROOT/vm/sync-secrets.sh" /opt/multistream/bin/sync-secrets.sh
install -m 755 "$ROOT/vm/refresh-ui-env.sh" /opt/multistream/bin/refresh-ui-env.sh
install -m 755 "$ROOT/vm/record-session.py" /opt/multistream/bin/record-session.py
install -m 644 "$ROOT/vm/mediamtx.yml" /opt/multistream/etc/mediamtx.yml
install -m 644 "$ROOT/vm/logrotate-multistream" /etc/logrotate.d/multistream

echo "==> UI"
rsync -a --delete --exclude '.venv' "$ROOT/ui/" /opt/multistream/ui/
/opt/multistream/ui/.venv/bin/pip install -q -r /opt/multistream/ui/requirements.txt

mkdir -p /var/log/multistream
chmod 700 /var/log/multistream

echo "==> Restart"
/opt/multistream/bin/refresh-ui-env.sh
systemctl restart multistream-ui.service
systemctl restart mediamtx.service

sleep 2
systemctl is-active multistream-ui.service mediamtx.service
echo "=== Update complete ==="
