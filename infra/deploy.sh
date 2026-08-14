#!/usr/bin/env bash
# Deploy Azure resources for MultiStreamCustom.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RG="${RG:-rg-multistream}"
LOCATION="${LOCATION:-centralindia}"
ADMIN_USER="${ADMIN_USER:-multistream}"

if [[ -z "${SSH_PUBLIC_KEY:-}" ]]; then
  if [[ -f "$HOME/.ssh/id_ed25519.pub" ]]; then
    SSH_PUBLIC_KEY="$(cat "$HOME/.ssh/id_ed25519.pub")"
  elif [[ -f "$HOME/.ssh/id_rsa.pub" ]]; then
    SSH_PUBLIC_KEY="$(cat "$HOME/.ssh/id_rsa.pub")"
  else
    echo "Set SSH_PUBLIC_KEY or create ~/.ssh/id_ed25519.pub" >&2
    exit 1
  fi
fi

if [[ -z "${UI_PIN:-}" ]]; then
  echo "Set UI_PIN to a numeric PIN your team will use (e.g. export UI_PIN=123456)" >&2
  exit 1
fi

echo "Creating resource group $RG in $LOCATION..."
az group create --name "$RG" --location "$LOCATION" --output none

echo "Deploying Bicep..."
DEPLOY_NAME="multistream"
az deployment group create \
  --name "$DEPLOY_NAME" \
  --resource-group "$RG" \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters \
    location="$LOCATION" \
    adminUsername="$ADMIN_USER" \
    sshPublicKey="$SSH_PUBLIC_KEY" \
    uiPin="$UI_PIN" \
  --output none

FQDN=$(az deployment group show -g "$RG" -n "$DEPLOY_NAME" --query properties.outputs.fqdn.value -o tsv)
PIP=$(az deployment group show -g "$RG" -n "$DEPLOY_NAME" --query properties.outputs.publicIpAddress.value -o tsv)
KV=$(az deployment group show -g "$RG" -n "$DEPLOY_NAME" --query properties.outputs.keyVaultName.value -o tsv)
VM=$(az deployment group show -g "$RG" -n "$DEPLOY_NAME" --query properties.outputs.vmName.value -o tsv)

echo ""
echo "=== Deployed ==="
echo "Resource group: $RG"
echo "VM:             $VM"
echo "Public IP:      $PIP"
echo "FQDN:           ${FQDN:-n/a}"
echo "Key Vault:      $KV"
echo ""
echo "Next: copy project to VM and run install:"
echo "  scp -r \"$ROOT\" ${ADMIN_USER}@${PIP}:~/MultiStreamCustom"
echo "  ssh ${ADMIN_USER}@${PIP}"
echo "  cd ~/MultiStreamCustom && sudo bash vm/install.sh --key-vault $KV --location $LOCATION"
