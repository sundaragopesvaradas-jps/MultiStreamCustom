#!/usr/bin/env bash
# Migrate plaintext ui-pin → ui-pin-hash (pbkdf2) in Key Vault.
set -euo pipefail

CONFIG_ENV="/opt/multistream/etc/multistream.env"
# shellcheck disable=SC1090
source "$CONFIG_ENV"

az login --identity --output none 2>/dev/null || az login --identity --output none

EXISTING_HASH="$(az keyvault secret show --vault-name "$KEY_VAULT_NAME" --name ui-pin-hash --query value -o tsv 2>/dev/null || true)"
if [[ -n "$EXISTING_HASH" && "$EXISTING_HASH" == pbkdf2_sha256\$* ]]; then
  echo "ui-pin-hash already set"
  exit 0
fi

PIN="$(az keyvault secret show --vault-name "$KEY_VAULT_NAME" --name ui-pin --query value -o tsv)"
if [[ -z "$PIN" || "$PIN" == "MOVED_TO_HASH" ]]; then
  echo "No plaintext PIN to migrate" >&2
  exit 1
fi

HASH="$(UI_PIN="$PIN" python3 - <<'PY'
import hashlib, os, secrets
pin = os.environ["UI_PIN"].strip().encode()
salt = secrets.token_bytes(16)
iters = 260000
digest = hashlib.pbkdf2_hmac("sha256", pin, salt, iters)
print(f"pbkdf2_sha256${iters}${salt.hex()}${digest.hex()}")
PY
)"

az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name ui-pin-hash --value "$HASH" --output none
az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name ui-pin --value "MOVED_TO_HASH" --output none
echo "migrated ui-pin → ui-pin-hash"
