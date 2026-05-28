#!/usr/bin/env bash
set -e

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
echo "Note: Initial model compilation on CPU can take 3-5 minutes..."
kubectl wait --for=condition=Ready pod -l app=vllm-cpu-server --timeout=600s

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
