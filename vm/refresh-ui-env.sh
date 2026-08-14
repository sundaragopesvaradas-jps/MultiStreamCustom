#!/usr/bin/env bash
# Load UI PIN from Key Vault into ui.env for the Flask app.
set -euo pipefail

CONFIG_ENV="/opt/multistream/etc/multistream.env"
# shellcheck disable=SC1090
source "$CONFIG_ENV"

az login --identity --output none 2>/dev/null || az login --identity --output none
PIN="$(az keyvault secret show --vault-name "$KEY_VAULT_NAME" --name ui-pin --query value -o tsv)"
SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"

# Keep Flask secret stable across restarts if already present
if [[ -f /opt/multistream/etc/ui.env ]] && grep -q '^FLASK_SECRET_KEY=' /opt/multistream/etc/ui.env; then
  # shellcheck disable=SC1091
  source /opt/multistream/etc/ui.env
  SECRET="${FLASK_SECRET_KEY}"
fi

umask 077
cat > /opt/multistream/etc/ui.env <<EOF
UI_PIN=${PIN}
FLASK_SECRET_KEY=${SECRET}
KEY_VAULT_NAME=${KEY_VAULT_NAME}
PUBLIC_HOST=${PUBLIC_HOST}
EOF
