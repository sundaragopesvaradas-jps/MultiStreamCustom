#!/usr/bin/env bash
# Install MediaMTX relay + PIN UI on the Azure VM.
set -euo pipefail

KEY_VAULT=""
LOCATION="centralindia"
PUBLIC_HOST=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key-vault) KEY_VAULT="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --public-host) PUBLIC_HOST="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$KEY_VAULT" ]]; then
  echo "Usage: sudo bash vm/install.sh --key-vault <name> [--location centralindia] [--public-host ip-or-fqdn]" >&2
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MTX_VERSION="${MTX_VERSION:-v1.11.3}"

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ffmpeg nginx python3 python3-venv python3-pip curl ca-certificates jq unzip rsync

if ! command -v az >/dev/null 2>&1; then
  echo "==> Azure CLI"
  curl -sL https://aka.ms/InstallAzureCLIDeb | bash
fi

echo "==> Azure login (managed identity)"
az login --identity --output none
az configure --defaults location="$LOCATION"

if [[ -z "$PUBLIC_HOST" ]]; then
  PUBLIC_HOST="$(curl -s -H Metadata:true \
    'http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text' || true)"
fi
if [[ -z "$PUBLIC_HOST" ]]; then
  PUBLIC_HOST="$(curl -s ifconfig.me || true)"
fi

echo "==> Directories"
mkdir -p /opt/multistream/{bin,etc,run,ui} /var/log/multistream
chmod 700 /opt/multistream/etc /opt/multistream/run

echo "==> MediaMTX ${MTX_VERSION}"
TMP="$(mktemp -d)"
curl -fsSL -o "$TMP/mediamtx.tar.gz" \
  "https://github.com/bluenviron/mediamtx/releases/download/${MTX_VERSION}/mediamtx_${MTX_VERSION}_linux_amd64.tar.gz"
tar -xzf "$TMP/mediamtx.tar.gz" -C "$TMP"
install -m 755 "$TMP/mediamtx" /opt/multistream/bin/mediamtx
rm -rf "$TMP"

echo "==> App files"
install -m 755 "$ROOT/vm/push-destinations.sh" /opt/multistream/bin/push-destinations.sh
install -m 755 "$ROOT/vm/stop-destinations.sh" /opt/multistream/bin/stop-destinations.sh
install -m 755 "$ROOT/vm/apply-destinations.sh" /opt/multistream/bin/apply-destinations.sh
install -m 755 "$ROOT/vm/on-stream-ready.sh" /opt/multistream/bin/on-stream-ready.sh
install -m 755 "$ROOT/vm/on-stream-end.sh" /opt/multistream/bin/on-stream-end.sh
install -m 755 "$ROOT/vm/sync-secrets.sh" /opt/multistream/bin/sync-secrets.sh
install -m 755 "$ROOT/vm/refresh-ui-env.sh" /opt/multistream/bin/refresh-ui-env.sh
install -m 755 "$ROOT/vm/record-session.py" /opt/multistream/bin/record-session.py
install -m 644 "$ROOT/vm/mediamtx.yml" /opt/multistream/etc/mediamtx.yml
install -m 644 "$ROOT/vm/logrotate-multistream" /etc/logrotate.d/multistream
if [[ ! -f /opt/multistream/etc/enabled.env ]]; then
  install -m 600 "$ROOT/vm/enabled.env" /opt/multistream/etc/enabled.env
fi

rsync -a --delete "$ROOT/ui/" /opt/multistream/ui/
python3 -m venv /opt/multistream/ui/.venv
/opt/multistream/ui/.venv/bin/pip install --upgrade pip
/opt/multistream/ui/.venv/bin/pip install -r /opt/multistream/ui/requirements.txt

umask 077
cat > /opt/multistream/etc/multistream.env <<EOF
KEY_VAULT_NAME=${KEY_VAULT}
PUBLIC_HOST=${PUBLIC_HOST}
AZURE_CONFIG_DIR=/opt/multistream/etc/azure
EOF
mkdir -p /opt/multistream/etc/azure
# Persist MI login for non-interactive services
az login --identity --output none
cp -a /root/.azure/. /opt/multistream/etc/azure/ 2>/dev/null || true

echo "==> Initial secret sync"
/opt/multistream/bin/sync-secrets.sh
/opt/multistream/bin/refresh-ui-env.sh

echo "==> nginx"
cp "$ROOT/vm/nginx-multistream.conf" /etc/nginx/sites-available/multistream
ln -sfn /etc/nginx/sites-available/multistream /etc/nginx/sites-enabled/multistream
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> systemd"
cp "$ROOT/vm/systemd/"*.service /etc/systemd/system/
cp "$ROOT/vm/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mediamtx.service
systemctl enable --now multistream-ui.service
systemctl enable --now multistream-sync.timer

INGEST_KEY="$(az keyvault secret show --vault-name "$KEY_VAULT" --name ingest-stream-key --query value -o tsv)"

echo ""
echo "=== Install complete ==="
echo "UI:          http://${PUBLIC_HOST}/"
echo "Zoom URL:    rtmp://${PUBLIC_HOST}/live"
echo "Zoom key:    ${INGEST_KEY}"
echo ""
echo "Open the UI, enter your PIN, paste YouTube + Facebook stream keys, then start Custom Live Streaming in Zoom."
