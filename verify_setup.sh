#!/usr/bin/env bash
set -e

# Load config and target the cluster named in .env (so we verify the right one).
if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found."
  exit 1
fi
echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}), backend=${BACKEND:-cpu} ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"

echo "=== Discovering Application Service IP ==="
echo "Waiting for Service gateway-client-app-svc to receive an IP..."
for i in {1..30}; do
  APP_IP=$(kubectl get svc gateway-client-app-svc -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [ -n "$APP_IP" ]; then
    echo "Found Application IP: $APP_IP"
    break
  fi
  sleep 10
done

if [ -z "$APP_IP" ]; then
  echo "Error: Service did not receive an IP within 5 minutes."
  exit 1
fi

echo "=== Waiting for vLLM Backend Pods to Warm Up ==="
echo "Note: first start can take several minutes (CPU compile, or GPU node provisioning + image pull)..."
kubectl wait --for=condition=Ready pod -l app=vllm-server --timeout=900s

echo "=== Running Integration Tests ==="
export APP_IP="${APP_IP}"
export APP_PORT="80"

echo "Setting up Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install pytest requests --quiet

pytest tests/test_integration.py -v

echo "=== Verification Successful ==="
