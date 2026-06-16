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
# MPS needs hostIPC on the pod (concurrent kernels share the GPU more fluidly than
# TIME_SHARING's fixed per-pod slices). Derived from the sharing strategy.
if [ "$GPU_SHARING_STRATEGY" = "MPS" ]; then HOST_IPC="true"; else HOST_IPC="false"; fi
# Namespace the manifests render into. Standalone runs in 'default'; the Hub overrides this
# with the feature's own namespace. Templating ${NAMESPACE} (e.g. EPP --pool-namespace) keeps
# the same manifests working in both.
NAMESPACE="${NAMESPACE:-default}"
export NAMESPACE BACKEND BLOCK_SIZE REPLICAS GPU_MEM_UTIL GPU_SHARING_STRATEGY GPU_MAX_SHARED GATEWAY_CLASS HOST_IPC
echo "GPU demo: ${REPLICAS} pods sharing 1 GPU (${GPU_SHARING_STRATEGY}, max ${GPU_MAX_SHARED}/GPU),"
echo "  block_size=${BLOCK_SIZE}, gpu_mem_util=${GPU_MEM_UTIL}, gateway_class=${GATEWAY_CLASS}"
if [ "${GPU_MAX_SHARED}" -lt "${REPLICAS}" ]; then
  echo "Error: GPU_MAX_SHARED (${GPU_MAX_SHARED}) must be >= REPLICAS (${REPLICAS})."; exit 1
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
  render infra/http-route.yaml | kubectl delete -f - --ignore-not-found || true
  kubectl delete -f infra/inferencepool.yaml --ignore-not-found || true
  kubectl delete -f infra/epp-rbac.yaml --ignore-not-found || true
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

echo "=== Step 6: Deploying InferencePool + EPP RBAC + HTTPRoute (plain manifests, no Helm) ==="
export INFERENCE_POOL_NAME=vllm-server
# EPP ServiceAccount + RBAC the llm-d EPP runs as.
kubectl apply -f infra/epp-rbac.yaml
# InferencePool (endpointPickerRef already points at the llm-d EPP) + GKE policies.
kubectl apply -f infra/inferencepool.yaml
# HTTPRoute attaching ${GATEWAY_NAME} to the InferencePool (parentRef baked in; no patch).
render infra/http-route.yaml | kubectl apply -f -

echo "=== Step 7: Deploying llm-d precise prefix-cache routing EPP ==="
# Event-driven precise prefix-cache routing: this EPP consumes vLLM's KV-cache events
# over ZMQ (enabled in the model-server deployment) and routes each request to the pod
# that physically holds the most of its prefix. It is the InferencePool's endpointPicker
# (set declaratively in infra/inferencepool.yaml — no kubectl patch).
export MODEL_NAME BLOCK_SIZE
render infra/llm-d-epp.yaml | kubectl apply -f -
kubectl rollout status deployment/vllm-server-epp-llmd --timeout=180s

echo "=== Step 8: Declaring InferenceObjectives (request priority / criticality) ==="
kubectl apply -f infra/inference-objective.yaml

echo "=== Setup Complete ==="
echo "Waiting for Gateway to receive an external IP address (this may take 3-5 minutes)..."
echo "You can check the status by running: kubectl get gateway ${GATEWAY_NAME}"
