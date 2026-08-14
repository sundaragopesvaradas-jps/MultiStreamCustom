#!/usr/bin/env bash
# Load UI auth material from Key Vault into ui.env for the Flask app.
set -euo pipefail

CONFIG_ENV="/opt/multistream/etc/multistream.env"
# shellcheck disable=SC1090
source "$CONFIG_ENV"

az login --identity --output none 2>/dev/null || az login --identity --output none

PIN_HASH=""
if az keyvault secret show --vault-name "$KEY_VAULT_NAME" --name ui-pin-hash --query value -o tsv >/tmp/ms-pin-hash 2>/dev/null; then
  PIN_HASH="$(cat /tmp/ms-pin-hash)"
fi
rm -f /tmp/ms-pin-hash

PIN_PLAIN=""
if [[ -z "$PIN_HASH" || "$PIN_HASH" == "MOVED_TO_HASH" ]]; then
  PIN_PLAIN="$(az keyvault secret show --vault-name "$KEY_VAULT_NAME" --name ui-pin --query value -o tsv 2>/dev/null || true)"
  if [[ "$PIN_PLAIN" == "MOVED_TO_HASH" ]]; then
    PIN_PLAIN=""
  fi
fi

SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
if [[ -f /opt/multistream/etc/ui.env ]]; then
  EXISTING_SECRET="$(
    python3 -c '
from pathlib import Path
for line in Path("/opt/multistream/etc/ui.env").read_text().splitlines():
    if line.startswith("FLASK_SECRET_KEY="):
        v = line.split("=", 1)[1].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'\''":
            v = v[1:-1]
        print(v)
        break
'
  )"
  if [[ -n "$EXISTING_SECRET" ]]; then
    SECRET="$EXISTING_SECRET"
  fi
fi

HTTPS_ENABLED=0
FQDN_CERT="/etc/letsencrypt/live/multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com/fullchain.pem"
if [[ -f "$FQDN_CERT" ]] || [[ -f "/etc/letsencrypt/live/${PUBLIC_HOST}/fullchain.pem" ]]; then
  HTTPS_ENABLED=1
fi

export PIN_HASH PIN_PLAIN SECRET KEY_VAULT_NAME PUBLIC_HOST HTTPS_ENABLED
umask 077
python3 - <<'PY'
import os
from pathlib import Path

def q(v: str) -> str:
    return "'" + v.replace("'", "'\"'\"'") + "'"

lines = [
    f"UI_PIN_HASH={q(os.environ.get('PIN_HASH', ''))}",
    f"UI_PIN={q(os.environ.get('PIN_PLAIN', ''))}",
    f"FLASK_SECRET_KEY={q(os.environ['SECRET'])}",
    f"KEY_VAULT_NAME={q(os.environ['KEY_VAULT_NAME'])}",
    f"PUBLIC_HOST={q(os.environ.get('PUBLIC_HOST', ''))}",
    f"HTTPS_ENABLED={q(os.environ.get('HTTPS_ENABLED', '0'))}",
    "",
]
Path("/opt/multistream/etc/ui.env").write_text("\n".join(lines))
print("wrote /opt/multistream/etc/ui.env")
PY
