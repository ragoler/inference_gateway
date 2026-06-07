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

# Point kubectl at the cluster named in .env so we never deploy to the wrong
# cluster (e.g. an existing demo cluster) based on the ambient context.
echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}) ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"

# The app image is backend-agnostic (one build for both clusters); the CPU/GPU
# difference is the BACKEND env injected into the Deployment below.
echo "=== Creating Artifact Registry Repository ==="
gcloud artifacts repositories create "$REPO_NAME" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Inference Gateway Client App Repo" || echo "Repo might already exist."

echo "=== Authenticating Docker ==="
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "=== Building Application Image (linux/amd64 for GKE nodes) ==="
docker build --platform linux/amd64 -t "${REGISTRY}/gateway-client-app:latest" ./app

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
export BACKEND="${BACKEND:-cpu}"   # shown in the UI; one backend per cluster

kubectl apply -f k8s/rbac.yaml
# Portable variable substitution (envsubst is not installed by default on macOS).
python3 -c "import os,sys; sys.stdout.write(os.path.expandvars(open('k8s/app-deployment.yaml').read()))" | kubectl apply -f -
kubectl apply -f k8s/app-service.yaml

# The image tag stays :latest, so an unchanged Deployment spec won't trigger a
# new pull. Force a rollout to pick up the freshly pushed image.
echo "=== Rolling out new image ==="
kubectl rollout restart deployment/gateway-client-app
kubectl rollout status deployment/gateway-client-app --timeout=180s

echo "=== Application Deployed ==="
echo "You can discover the service IP by running: kubectl get svc gateway-client-app-svc"
