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

# ---------------------------------------------------------------------------
# Mode dispatch (parsed early so --help / bad args exit before any work).
#   (no flag)         create everything
#   --delete          tear down in-cluster resources (keep cluster, CRDs, subnet)
#   --delete-cluster  tear down resources AND delete the GKE cluster
# Run a delete mode, then a plain run, for a clean reproducible rebuild.
# The shared proxy-only subnet is NEVER deleted (other clusters use it).
# ---------------------------------------------------------------------------
MODE="create"
case "${1:-}" in
  --delete)         MODE="delete" ;;
  --delete-cluster) MODE="delete-cluster" ;;
  -h|--help)        echo "Usage: $0 [--delete | --delete-cluster]"; exit 0 ;;
  "")               MODE="create" ;;
  *) echo "Unknown argument: $1 (use --delete, --delete-cluster, or no flag)"; exit 1 ;;
esac

# ---------------------------------------------------------------------------
# GPU-only load-test comparison demo. All model-server replicas SHARE one GPU via
# the gpu-flex ComputeClass GPU-sharing config; an internal gke-l7-rilb gateway
# (llm-d EPP) and a plain vllm-direct Service form the two comparison arms.
#
#   BLOCK_SIZE            KV block size; MUST match vLLM --block-size <-> EPP blockSize
#   REPLICAS             # of vLLM pods sharing one GPU
#   GPU_MEM_UTIL          per-pod --gpu-memory-utilization (sum across REPLICAS < ~0.9)
#   GPU_SHARING_STRATEGY  TIME_SHARING | MPS  (MPS also needs hostIPC:true on the pod)
#   GPU_MAX_SHARED        ComputeClass maxSharedClientsPerGPU (must be >= REPLICAS)
#   GATEWAY_CLASS         gke-l7-rilb (internal, apples-to-apples) | gke-l7-regional-external-managed
# ---------------------------------------------------------------------------
BACKEND="gpu"
MODEL_DEPLOYMENT="infra/gpu-deployment.yaml"
COMPUTE_CLASS_FILE="infra/computeclass-gpu.yaml"
BLOCK_SIZE="${BLOCK_SIZE:-16}"
REPLICAS="${REPLICAS:-4}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.20}"
GPU_SHARING_STRATEGY="${GPU_SHARING_STRATEGY:-TIME_SHARING}"
GPU_MAX_SHARED="${GPU_MAX_SHARED:-4}"
GATEWAY_CLASS="${GATEWAY_CLASS:-gke-l7-rilb}"
export BACKEND BLOCK_SIZE REPLICAS GPU_MEM_UTIL GPU_SHARING_STRATEGY GPU_MAX_SHARED GATEWAY_CLASS
echo "GPU demo: ${REPLICAS} pods sharing 1 GPU (${GPU_SHARING_STRATEGY}, max ${GPU_MAX_SHARED}/GPU),"
echo "  block_size=${BLOCK_SIZE}, gpu_mem_util=${GPU_MEM_UTIL}, gateway_class=${GATEWAY_CLASS}"
if [ "${GPU_MAX_SHARED}" -lt "${REPLICAS}" ]; then
  echo "Error: GPU_MAX_SHARED (${GPU_MAX_SHARED}) must be >= REPLICAS (${REPLICAS})."; exit 1
fi

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

teardown_resources() {
  if ! gcloud container clusters describe "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" &>/dev/null; then
    echo "Cluster ${CLUSTER_NAME} does not exist; nothing to tear down."
    return 0
  fi
  echo "=== Tearing down in-cluster resources (backend=${BACKEND}) ==="
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"
  export MODEL_NAME GATEWAY_NAME KV_CACHE_SPACE BLOCK_SIZE INFERENCE_POOL_NAME=vllm-server
  kubectl delete -f infra/inference-objective.yaml --ignore-not-found || true
  render infra/llm-d-epp.yaml | kubectl delete -f - --ignore-not-found || true
  kubectl delete -f infra/vllm-direct.yaml --ignore-not-found || true
  helm uninstall "${INFERENCE_POOL_NAME}" || true
  render "${MODEL_DEPLOYMENT}" | kubectl delete -f - --ignore-not-found || true
  render infra/gateway.yaml | kubectl delete -f - --ignore-not-found || true
  render "${COMPUTE_CLASS_FILE}" | kubectl delete -f - --ignore-not-found || true
  echo "Resource teardown complete."
}

if [ "$MODE" = "delete" ] || [ "$MODE" = "delete-cluster" ]; then
  teardown_resources
  if [ "$MODE" = "delete-cluster" ]; then
    echo "=== Deleting GKE cluster ${CLUSTER_NAME} (this takes several minutes) ==="
    gcloud container clusters delete "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" --quiet || true
    echo "Note: the shared proxy-only subnet is left intact (other clusters use it)."
  fi
  echo "=== Teardown complete ==="
  exit 0
fi

echo "=== Step 1: Creating GKE Cluster ==="
# Node Auto-Provisioning lets the Custom Compute Class create node pools on demand
# (spot->on-demand, family fallback). The small default node pool (NUM_NODES) hosts
# the gateway controller, EPP, and the app; model servers land on ComputeClass nodes.
NAP_FLAGS="--enable-autoprovisioning --min-cpu 0 --max-cpu ${MAX_CPU:-200} --min-memory 0 --max-memory ${MAX_MEMORY:-2000}"
if [ "$BACKEND" = "gpu" ]; then
  # NAP must allow BOTH GPU types the gpu-flex ComputeClass can pick:
  # G4 = nvidia-rtx-pro-6000 (preferred), G2 = nvidia-l4 (fallback).
  NAP_FLAGS="${NAP_FLAGS} --max-accelerator type=${GPU_ACCELERATOR_TYPE:-nvidia-rtx-pro-6000},count=${MAX_GPU:-8}"
  NAP_FLAGS="${NAP_FLAGS} --max-accelerator type=${GPU_ACCELERATOR_TYPE_FALLBACK:-nvidia-l4},count=${MAX_GPU:-8}"
fi
if gcloud container clusters describe "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" &>/dev/null; then
  echo "Cluster ${CLUSTER_NAME} already exists. Skipping creation."
else
  gcloud container clusters create "${CLUSTER_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --num-nodes="${NUM_NODES}" \
    --gateway-api=standard \
    ${NAP_FLAGS}
fi

echo "=== Step 2: Getting Cluster Credentials ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}"

echo "=== Step 2b: Ensuring a proxy-only subnet exists (required for the regional gateway, internal or external) ==="
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

echo "=== Step 4b: Applying GPU Custom Compute Class (GPU sharing; spot -> on-demand, G4 -> G2) ==="
render "${COMPUTE_CLASS_FILE}" | kubectl apply -f -

echo "=== Step 5: Deploying ${REPLICAS} vLLM pods sharing one GPU ==="
export MODEL_NAME="${MODEL_NAME}"
export KV_CACHE_SPACE="${KV_CACHE_SPACE}"
render "${MODEL_DEPLOYMENT}" | kubectl apply -f -

echo "=== Step 5b: Applying vllm-direct Service (the 'without llm-d' round-robin baseline) ==="
kubectl apply -f infra/vllm-direct.yaml

echo "=== Step 6: Deploying InferencePool and EPP using Helm ==="
export INFERENCE_POOL_NAME=vllm-server
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

echo "=== Step 6b: Attaching the HTTPRoute to gateway ${GATEWAY_NAME} ==="
# The chart's experimentalHttpRoute hardcodes parentRef name "inference-gateway";
# repoint it at our actual gateway so a custom GATEWAY_NAME works (otherwise the
# route attaches to a non-existent gateway and the gateway returns 404).
kubectl patch httproute ${INFERENCE_POOL_NAME} --type merge \
  -p "{\"spec\":{\"parentRefs\":[{\"group\":\"gateway.networking.k8s.io\",\"kind\":\"Gateway\",\"name\":\"${GATEWAY_NAME}\"}]}}"

echo "=== Step 7: Tuning the vanilla EPP scorer weights (rollback target) ==="
# Tune the Helm-installed EPP's prefix-cache weight. This EPP is kept as a fast
# rollback target; the llm-d EPP in Step 8 becomes the active Endpoint Picker.
export INFERENCE_POOL_NAME
render infra/epp-config.yaml | kubectl apply -f -
kubectl rollout restart deployment/${INFERENCE_POOL_NAME}-epp
kubectl rollout status deployment/${INFERENCE_POOL_NAME}-epp --timeout=120s

echo "=== Step 8: Deploying llm-d precise prefix-cache routing EPP ==="
# Event-driven precise prefix-cache routing: this EPP consumes vLLM's KV-cache
# events over ZMQ (enabled in the model-server deployment) and routes each request
# to the pod that physically holds the most of its prefix. It becomes the active
# Endpoint Picker; flip endpointPickerRef back to ${INFERENCE_POOL_NAME}-epp to roll back.
export MODEL_NAME BLOCK_SIZE
render infra/llm-d-epp.yaml | kubectl apply -f -
kubectl rollout status deployment/vllm-server-epp-llmd --timeout=180s
kubectl patch inferencepool ${INFERENCE_POOL_NAME} --type merge \
  -p '{"spec":{"endpointPickerRef":{"name":"vllm-server-epp-llmd"}}}'

echo "=== Step 9: Declaring InferenceObjectives (request priority / criticality) ==="
kubectl apply -f infra/inference-objective.yaml

echo "=== Setup Complete ==="
echo "Waiting for Gateway to receive an external IP address (this may take 3-5 minutes)..."
echo "You can check the status by running: kubectl get gateway ${GATEWAY_NAME}"
