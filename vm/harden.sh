#!/usr/bin/env bash
# Apply Tier-1 hardening on an already-installed MultiStream VM.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FQDN="${FQDN:-multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com}"
LE_EMAIL="${LE_EMAIL:-sandipkumar.cse2017@gmail.com}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y fail2ban certbot python3-certbot-nginx

echo "==> App + deps"
rsync -a --delete --exclude '.venv' "$ROOT/ui/" /opt/multistream/ui/
/opt/multistream/ui/.venv/bin/pip install -r /opt/multistream/ui/requirements.txt

install -m 755 "$ROOT/vm/refresh-ui-env.sh" /opt/multistream/bin/refresh-ui-env.sh
install -m 755 "$ROOT/vm/migrate-pin-hash.sh" /opt/multistream/bin/migrate-pin-hash.sh
install -m 755 "$ROOT/vm/sync-secrets.sh" /opt/multistream/bin/sync-secrets.sh
install -m 755 "$ROOT/vm/push-destinations.sh" /opt/multistream/bin/push-destinations.sh
install -m 755 "$ROOT/vm/stop-destinations.sh" /opt/multistream/bin/stop-destinations.sh

echo "==> Migrate PIN to hash"
/opt/multistream/bin/migrate-pin-hash.sh

echo "==> nginx rate limits"
# limit_req_zone must live in http{} context
if ! grep -q 'zone=ms_login' /etc/nginx/nginx.conf; then
  sed -i '/http {/a\    limit_req_zone $binary_remote_addr zone=ms_login:10m rate=1r/s;\n    limit_req_status 429;' /etc/nginx/nginx.conf
fi
# Site config without duplicate zone (zone is in nginx.conf)
cat > /etc/nginx/sites-available/multistream <<EOF
upstream multistream_ui {
    server 127.0.0.1:8080;
    keepalive 8;
}

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${FQDN} 20.219.6.126 _;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type text/plain;
    }

    location = /login {
        limit_req zone=ms_login burst=5 nodelay;
        proxy_pass http://multistream_ui;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    location / {
        proxy_pass http://multistream_ui;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    location /healthz {
        proxy_pass http://multistream_ui/healthz;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/multistream /etc/nginx/sites-enabled/multistream
mkdir -p /var/www/html
nginx -t
systemctl reload nginx

echo "==> Let's Encrypt HTTPS"
if [[ ! -d "/etc/letsencrypt/live/${FQDN}" ]]; then
  certbot --nginx -d "$FQDN" \
    --non-interactive --agree-tos -m "$LE_EMAIL" \
    --redirect || echo "WARN: certbot failed — UI stays on HTTP until DNS/ports allow issuance"
else
  certbot renew --dry-run || true
fi

# Ensure proxy passes correct proto after certbot edits
if [[ -d "/etc/letsencrypt/live/${FQDN}" ]]; then
  # Force HTTPS_ENABLED for cookies
  :
fi

echo "==> fail2ban"
install -m 644 "$ROOT/vm/fail2ban/filter.d/multistream-ui.conf" /etc/fail2ban/filter.d/multistream-ui.conf
install -m 644 "$ROOT/vm/fail2ban/jail.d/multistream.conf" /etc/fail2ban/jail.d/multistream.conf
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "==> Refresh UI env + restart"
/opt/multistream/bin/refresh-ui-env.sh
# Mark HTTPS if cert exists
if [[ -d "/etc/letsencrypt/live/${FQDN}" ]]; then
  if grep -q '^HTTPS_ENABLED=' /opt/multistream/etc/ui.env; then
    sed -i 's/^HTTPS_ENABLED=.*/HTTPS_ENABLED=1/' /opt/multistream/etc/ui.env
  else
    echo 'HTTPS_ENABLED=1' >> /opt/multistream/etc/ui.env
  fi
fi
systemctl restart multistream-ui.service
systemctl restart mediamtx.service || true

echo ""
echo "=== Hardening complete ==="
if [[ -d "/etc/letsencrypt/live/${FQDN}" ]]; then
  echo "UI (HTTPS): https://${FQDN}/"
else
  echo "UI (HTTP):  http://20.219.6.126/"
fi
echo "PIN unchanged; now stored as hash in Key Vault (ui-pin-hash)."
