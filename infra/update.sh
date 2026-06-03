#!/usr/bin/env bash
# Update the running Container App to a new image built from the current source.
#
# This is the RECURRING deployment operation: build an image in ACR tagged with
# the git short SHA (the established convention) and point the Container App at
# it. Does NOT touch secrets or other env vars — existing config is preserved.
#
# Usage:
#   bash infra/update.sh                # tag = current git short SHA (recommended)
#   bash infra/update.sh <tag>          # build/deploy an explicit tag
#
# Prereqs: az CLI logged in (`az account show`), run from the repo root.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=infra/config.env
source infra/config.env

IMAGE_TAG="${1:-$(git rev-parse --short HEAD)}"
ACR_LOGIN="${ACR_NAME}.azurecr.io"
FULL_IMAGE="${ACR_LOGIN}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "=== Building ${IMAGE_NAME}:${IMAGE_TAG} in ACR ${ACR_NAME} ==="
# NOTE (Windows): `az acr build`'s client-side log streamer can crash with a
# cp950 UnicodeEncodeError on box-drawing chars. The build itself still runs and
# SUCCEEDS server-side. We tolerate a client crash here and verify the tag below
# rather than trusting this command's exit code on Windows.
az acr build --registry "$ACR_NAME" --image "${IMAGE_NAME}:${IMAGE_TAG}" --file Dockerfile . || \
  echo "WARN: az acr build client exited non-zero (often the Windows cp950 log-stream bug) — verifying the tag instead..."

echo "=== Verifying image tag exists in registry ==="
if ! az acr repository show-tags --name "$ACR_NAME" --repository "$IMAGE_NAME" -o tsv | grep -qx "$IMAGE_TAG"; then
  echo "ERROR: image tag '${IMAGE_TAG}' not found in ${ACR_NAME} — build did not produce the image." >&2
  exit 1
fi

echo "=== Updating Container App ${APP_NAME} -> ${FULL_IMAGE} ==="
az containerapp update \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --image "$FULL_IMAGE" \
  --only-show-errors -o none

APP_URL=$(az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query "properties.configuration.ingress.fqdn" -o tsv)
echo "=== Done. Deployed ${IMAGE_TAG} ==="
echo "Health: https://${APP_URL}/health"
echo "MCP:    https://${APP_URL}/mcp"
echo "Verify: curl -s https://${APP_URL}/health   # expect {\"status\":\"ok\"}"
