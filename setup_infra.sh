#!/usr/bin/env bash
set -e

# Load configuration
if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found. Please create one from .env.example."
  exit 1
fi

# Ensure required CLI tools are installed (we substitute env vars with python3,
# so envsubst is NOT required -- keeps this runnable on macOS and Linux).
for cmd in gcloud kubectl python3; do
  if ! command -v $cmd &> /dev/null; then
    echo "Error: $cmd is required but not installed."
    exit 1
  fi
done

# Portable env-var substitution for manifests (replaces ${VAR}; leaves $(VAR)
# alone so Kubernetes downward-API refs like $(POD_IP) survive to runtime).
render() { python3 -c "import os,sys;sys.stdout.write(os.path.expandvars(open(sys.argv[1]).read()))" "$1"; }

# Check and install helm locally if needed (OS/arch-aware download).
if ! command -v helm &> /dev/null; then
  echo "helm not found. Installing locally to ./bin..."
  mkdir -p bin
  HELM_OS=$(uname -s | tr '[:upper:]' '[:lower:]')          # darwin | linux
  HELM_ARCH=$(uname -m); case "$HELM_ARCH" in x86_64) HELM_ARCH=amd64;; arm64|aarch64) HELM_ARCH=arm64;; esac
  curl -fsSL "https://get.helm.sh/helm-v3.15.1-${HELM_OS}-${HELM_ARCH}.tar.gz" \
    | tar -xz -C bin --strip-components=1 "${HELM_OS}-${HELM_ARCH}/helm"
  export PATH="$PWD/bin:$PATH"
fi

echo "=== Step 1: Creating GKE Cluster ==="
if gcloud container clusters describe "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "Cluster ${CLUSTER_NAME} already exists. Skipping creation."
else
  gcloud container clusters create "${CLUSTER_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --num-nodes="${NUM_NODES}" \
    --gateway-api=standard
fi

echo "=== Step 2: Getting Cluster Credentials ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}"

echo "=== Step 2b: Ensuring a proxy-only subnet exists (required for the regional external gateway) ==="
REGION="${REGION:-${ZONE%-*}}"
if gcloud compute networks subnets list --project="${PROJECT_ID}" \
     --filter="purpose=REGIONAL_MANAGED_PROXY AND region:${REGION}" --format="value(name)" | grep -q .; then
  echo "Proxy-only subnet already present in ${REGION}; skipping."
else
  gcloud compute networks subnets create igw-proxy-only-subnet \
    --project="${PROJECT_ID}" --region="${REGION}" --network=default \
    --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE --range=192.168.20.0/23
fi

echo "=== Step 3: Installing Inference Extension CRDs (pinned) ==="
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"
kubectl apply -k "https://github.com/kubernetes-sigs/gateway-api-inference-extension/config/crd?ref=${GAIE_VERSION}"

echo "=== Step 4: Deploying GKE Inference Gateway ==="
export GATEWAY_NAME="${GATEWAY_NAME}"
render infra/gateway.yaml | kubectl apply -f -

echo "=== Step 5: Deploying CPU-Based vLLM Model Server ==="
export MODEL_NAME="${MODEL_NAME}"
export KV_CACHE_SPACE="${KV_CACHE_SPACE}"
render infra/cpu-deployment.yaml | kubectl apply -f -

echo "=== Step 6: Deploying InferencePool and EPP using Helm ==="
export INFERENCE_POOL_NAME=vllm-cpu-server
export GATEWAY_PROVIDER=gke
export MODEL_SERVER=vllm
export MODEL_SERVER_PROTOCOL=http
export IGW_CHART_VERSION=v0

helm upgrade --install ${INFERENCE_POOL_NAME} \
 --set inferencePool.modelServers.matchLabels.app=${INFERENCE_POOL_NAME} \
 --set provider.name=${GATEWAY_PROVIDER} \
 --set inferencePool.modelServerType=${MODEL_SERVER} \
 --set inferencePool.modelServerProtocol=${MODEL_SERVER_PROTOCOL} \
 --set experimentalHttpRoute.enabled=true \
 --version ${IGW_CHART_VERSION} \
 oci://us-central1-docker.pkg.dev/k8s-staging-images/gateway-api-inference-extension/charts/inferencepool

echo "=== Step 7: Tuning the vanilla EPP scorer weights (rollback target) ==="
# Tune the Helm-installed EPP's prefix-cache weight. This EPP is kept as a fast
# rollback target; the llm-d EPP in Step 8 becomes the active Endpoint Picker.
export INFERENCE_POOL_NAME
render infra/epp-config.yaml | kubectl apply -f -
kubectl rollout restart deployment/${INFERENCE_POOL_NAME}-epp
kubectl rollout status deployment/${INFERENCE_POOL_NAME}-epp --timeout=120s

echo "=== Step 8: Deploying llm-d precise prefix-cache routing EPP ==="
# Event-driven precise prefix-cache routing: this EPP consumes vLLM's KV-cache
# events over ZMQ (enabled in infra/cpu-deployment.yaml) and routes each request
# to the pod that physically holds the most of its prefix. It becomes the active
# Endpoint Picker; flip endpointPickerRef back to ${INFERENCE_POOL_NAME}-epp to roll back.
export MODEL_NAME
render infra/llm-d-epp.yaml | kubectl apply -f -
kubectl rollout status deployment/vllm-cpu-server-epp-llmd --timeout=180s
kubectl patch inferencepool ${INFERENCE_POOL_NAME} --type merge \
  -p '{"spec":{"endpointPickerRef":{"name":"vllm-cpu-server-epp-llmd"}}}'

echo "=== Step 9: Declaring InferenceObjectives (request priority / criticality) ==="
kubectl apply -f infra/inference-objective.yaml

echo "=== Setup Complete ==="
echo "Waiting for Gateway to receive an external IP address (this may take 3-5 minutes)..."
echo "You can check the status by running: kubectl get gateway ${GATEWAY_NAME}"
