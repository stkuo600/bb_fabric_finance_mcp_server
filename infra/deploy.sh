#!/usr/bin/env bash
# First-time / disaster-recovery deployment of the Container App.
#
# For routine code updates use infra/update.sh instead — this script recreates
# the app from scratch (resource group, ACR, environment, app + secrets).
#
# Secrets are read from your shell environment and injected as Container App
# *secrets* (referenced via secretref), NOT committed. Export these first:
#   FABRIC_SERVER FABRIC_DATABASE FABRIC_CLIENT_ID FABRIC_CLIENT_SECRET
#   FABRIC_TENANT_ID FABRIC_API_KEY FABRIC_WRITE_ALLOWLIST
#   FABRIC_TOKEN_SIGNING_KEY   # long random string, stable across replicas
#
# Prereqs: az CLI logged in; run from the repo root.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=infra/config.env
source infra/config.env

IMAGE_TAG="${1:-$(git rev-parse --short HEAD)}"
ACR_LOGIN="${ACR_NAME}.azurecr.io"

: "${FABRIC_SERVER:?set FABRIC_SERVER}"
: "${FABRIC_DATABASE:?set FABRIC_DATABASE}"
: "${FABRIC_CLIENT_ID:?set FABRIC_CLIENT_ID}"
: "${FABRIC_CLIENT_SECRET:?set FABRIC_CLIENT_SECRET}"
: "${FABRIC_TENANT_ID:?set FABRIC_TENANT_ID}"
: "${FABRIC_API_KEY:?set FABRIC_API_KEY}"
: "${FABRIC_WRITE_ALLOWLIST:?set FABRIC_WRITE_ALLOWLIST}"
: "${FABRIC_TOKEN_SIGNING_KEY:?set FABRIC_TOKEN_SIGNING_KEY (e.g. python -c 'import secrets;print(secrets.token_urlsafe(48))')}"

echo "=== Ensure resource group ==="
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none

echo "=== Ensure ACR ==="
az acr show --name "$ACR_NAME" -o none 2>/dev/null || \
  az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --sku Basic --admin-enabled true -o none

echo "=== Build image ${IMAGE_NAME}:${IMAGE_TAG} (see Windows cp950 note in update.sh) ==="
az acr build --registry "$ACR_NAME" --image "${IMAGE_NAME}:${IMAGE_TAG}" --file Dockerfile . || true
az acr repository show-tags --name "$ACR_NAME" --repository "$IMAGE_NAME" -o tsv | grep -qx "$IMAGE_TAG" \
  || { echo "ERROR: image ${IMAGE_TAG} not in registry" >&2; exit 1; }

echo "=== Ensure Container Apps environment ==="
az extension add --name containerapp --upgrade --only-show-errors 2>/dev/null || true
az containerapp env show --name "$ENVIRONMENT_NAME" --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null || \
  az containerapp env create --name "$ENVIRONMENT_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" -o none

echo "=== Create Container App ==="
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)
az containerapp create \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --environment "$ENVIRONMENT_NAME" \
  --image "${ACR_LOGIN}/${IMAGE_NAME}:${IMAGE_TAG}" \
  --registry-server "$ACR_LOGIN" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASSWORD" \
  --target-port "$TARGET_PORT" \
  --ingress external \
  --min-replicas "$MIN_REPLICAS" \
  --max-replicas "$MAX_REPLICAS" \
  --cpu "$CPU" \
  --memory "$MEMORY" \
  --secrets \
    fabric-client-secret="$FABRIC_CLIENT_SECRET" \
    fabric-api-key="$FABRIC_API_KEY" \
    fabric-token-signing-key="$FABRIC_TOKEN_SIGNING_KEY" \
  --env-vars \
    FABRIC_SERVER="$FABRIC_SERVER" \
    FABRIC_DATABASE="$FABRIC_DATABASE" \
    FABRIC_CLIENT_ID="$FABRIC_CLIENT_ID" \
    FABRIC_CLIENT_SECRET=secretref:fabric-client-secret \
    FABRIC_TENANT_ID="$FABRIC_TENANT_ID" \
    FABRIC_API_KEY=secretref:fabric-api-key \
    FABRIC_TOKEN_SIGNING_KEY=secretref:fabric-token-signing-key \
    FABRIC_WRITE_ALLOWLIST="$FABRIC_WRITE_ALLOWLIST" \
    FABRIC_PORT="$TARGET_PORT" \
  --only-show-errors -o none

echo "=== Configure health probes ==="
az containerapp update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --yaml infra/probe-config.yaml --only-show-errors -o none || \
  echo "WARN: probe config step failed; app is up but probes may be default."

APP_URL=$(az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query "properties.configuration.ingress.fqdn" -o tsv)
echo "=== Deployed at https://${APP_URL} ==="
