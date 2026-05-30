#!/usr/bin/env bash
set -e

if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found."
  exit 1
fi

REGION="${REGION:-${ZONE%-*}}"
REPO_NAME="inference-gateway-client"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}"

echo "=== Creating Artifact Registry Repository ==="
gcloud artifacts repositories create "$REPO_NAME" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Inference Gateway Client App Repo" || echo "Repo might already exist."

echo "=== Authenticating Docker ==="
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "=== Building Application Image ==="
docker build -t "${REGISTRY}/gateway-client-app:latest" ./app

echo "=== Pushing Application Image ==="
docker push "${REGISTRY}/gateway-client-app:latest"

echo "=== Discovering Gateway IP ==="
echo "Waiting for Gateway ${GATEWAY_NAME} to receive an IP..."
for i in {1..30}; do
  GATEWAY_IP=$(kubectl get gateway "${GATEWAY_NAME}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  if [ -n "$GATEWAY_IP" ]; then
    echo "Found Gateway IP: $GATEWAY_IP"
    break
  fi
  sleep 10
done

if [ -z "$GATEWAY_IP" ]; then
  echo "Error: Gateway did not receive an IP within 5 minutes."
  exit 1
fi

echo "=== Deploying Application Manifests ==="
export REGISTRY="${REGISTRY}"
export GATEWAY_IP="${GATEWAY_IP}"
export MODEL_NAME="${MODEL_NAME}"

kubectl apply -f k8s/rbac.yaml
envsubst < k8s/app-deployment.yaml | kubectl apply -f -
kubectl apply -f k8s/app-service.yaml

echo "=== Application Deployed ==="
echo "You can discover the service IP by running: kubectl get svc gateway-client-app-svc"
