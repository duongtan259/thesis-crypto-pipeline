#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# Azure setup script — run once after creating your Azure account
#
# Deploys via Bicep (infra/main.bicep):
#   VNet + subnets → Key Vault → Event Hub (private) →
#   Managed Identity → Private Endpoints → RBAC assignments
#
# Auth model: Managed Identity + OIDC — no connection strings in code
# ════════════════════════════════════════════════════════════════
set -euo pipefail
trap 'echo ""; echo "ERROR: Setup failed at line $LINENO — check output above"; exit 1' ERR

RG="rg-thesis-fabric"
LOCATION="westeurope"
PREFIX="thesis-crypto"

echo "==> Logging in to Azure..."
az login

echo "==> Available subscriptions:"
az account list --output table

echo ""
read -rp "Enter your subscription ID: " SUBSCRIPTION_ID
az account set --subscription "$SUBSCRIPTION_ID"

echo ""
echo "==> Creating resource group: $RG in $LOCATION"
az group create --name "$RG" --location "$LOCATION" --output none

echo "==> Deploying infrastructure via Bicep (VNet, Key Vault, Event Hub, Managed Identity)..."
DEPLOY_OUT=$(az deployment group create \
  --resource-group "$RG" \
  --template-file "$(dirname "$0")/../infra/main.bicep" \
  --parameters "$(dirname "$0")/../infra/parameters.json" \
  --output json)

# Extract outputs
EH_FQDN=$(echo "$DEPLOY_OUT"           | python3 -c "import sys,json; print(json.load(sys.stdin)['properties']['outputs']['eventHubFqdn']['value'])")
KV_URI=$(echo "$DEPLOY_OUT"            | python3 -c "import sys,json; print(json.load(sys.stdin)['properties']['outputs']['keyVaultUri']['value'])")
IDENTITY_CLIENT_ID=$(echo "$DEPLOY_OUT"| python3 -c "import sys,json; print(json.load(sys.stdin)['properties']['outputs']['generatorIdentityClientId']['value'])")
IDENTITY_ID=$(echo "$DEPLOY_OUT"       | python3 -c "import sys,json; print(json.load(sys.stdin)['properties']['outputs']['generatorIdentityId']['value'])")
ACI_SUBNET_ID=$(echo "$DEPLOY_OUT"     | python3 -c "import sys,json; print(json.load(sys.stdin)['properties']['outputs']['aciSubnetId']['value'])")

echo ""
echo "==> Storing Event Hub namespace in Key Vault (so generator fetches it at runtime)..."
az keyvault secret set \
  --vault-name "${PREFIX}-kv" \
  --name "eventhub-namespace" \
  --value "$EH_FQDN" \
  --output none

echo "==> Infrastructure deployed successfully."
echo ""
echo "════════════════════════════════════════════════════════"
echo "  DEPLOYMENT COMPLETE"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  Auth model:         Managed Identity (OIDC — no secrets)"
echo "  Event Hub FQDN:     $EH_FQDN"
echo "  Key Vault URI:      $KV_URI"
echo "  Identity Client ID: $IDENTITY_CLIENT_ID"
echo "  ACI Subnet ID:      $ACI_SUBNET_ID"
echo ""
echo "  Next: deploy the generator container to ACI:"
echo ""
echo "  az container create \\"
echo "    --resource-group $RG \\"
echo "    --name crypto-generator \\"
echo "    --image thesiscryptoacr.azurecr.io/crypto-generator:latest \\"
echo "    --assign-identity $IDENTITY_ID \\"
echo "    --subnet $ACI_SUBNET_ID \\"
echo "    --environment-variables \\"
echo "        USE_LOCAL_KAFKA=false \\"
echo "        DATA_SOURCE=merged \\"
echo "        SYMBOLS=BTC-USD,ETH-USD,SOL-USD,BNB-USD,XRP-USD \\"
echo "        EVENTHUB_NAMESPACE=$EH_FQDN \\"
echo "        EVENTHUB_NAME=crypto-prices \\"
echo "    --cpu 1 --memory 1.5 \\"
echo "    --restart-policy Always"
echo ""
echo "  No secrets. No .env. The container uses Managed Identity to"
echo "  authenticate to Event Hub via OIDC automatically."
echo "════════════════════════════════════════════════════════"
