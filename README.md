# Inference Gateway on GKE — llm-d Load-Test Comparison

This repository provisions a GKE cluster with the Kubernetes Gateway API Inference
Extension and **llm-d precise prefix-cache routing**, runs several vLLM model
servers **sharing one GPU**, and serves a FastAPI app + UI that proves the value
of cache-aware routing with a **side-by-side load test**: the same concurrent
workload run **with llm-d** (cache-aware) vs **without llm-d** (a plain round-robin
Service).

> A single request on a fast GPU barely shows a difference. Under load it is
> obvious: cache-blind routing scatters repeated documents across pods and
> re-prefills them everywhere; llm-d routes each document's repeats to the pod
> that already holds its prefix, so the shared GPU wastes far less compute.

---

## 🏗️ Architecture Overview

1. **Infrastructure**: a GKE cluster with the Gateway API and Node
   Auto-Provisioning. A small on-demand default pool hosts the gateway/EPP/app;
   the vLLM model servers run on the `gpu-flex` Custom Compute Class.
2. **Inference**:
   - **Internal L7 Gateway** (`gke-l7-rilb`) so the llm-d path is in-cluster,
     apples-to-apples with the in-cluster baseline.
   - **llm-d Endpoint Picker (EPP)** — precise prefix-cache routing from vLLM
     KV-cache events (the "with llm-d" arm).
   - **`vllm-direct` ClusterIP Service** — cache-blind round-robin (the "without
     llm-d" arm).
   - **vLLM GPU model servers** — `REPLICAS` pods **sharing one physical GPU** via
     GKE GPU sharing.
3. **Application**: a containerized FastAPI service that runs the comparison load
   test, streams telemetry, and serves the UI.

---

## 📁 Repository Structure

```
├── .env.example              # Template for configuration environment variables
├── app/
│   ├── Dockerfile            # Containerization for the FastAPI service
│   ├── main.py               # FastAPI: /generate, /api/loadtest, telemetry, status
│   ├── loadtest_util.py      # Pure (unit-tested) load-test helpers
│   ├── requirements.txt      # Python runtime dependencies
│   └── static/               # UI (index.html, app.js)
├── infra/
│   ├── gpu-deployment.yaml   # vLLM model servers, REPLICAS pods sharing one GPU
│   ├── computeclass-gpu.yaml # gpu-flex ComputeClass (G4→G2, spot→on-demand, gpuSharing)
│   ├── gateway.yaml          # Internal L7 Gateway (gke-l7-rilb by default)
│   ├── vllm-direct.yaml      # Plain ClusterIP Service — the "without llm-d" baseline
│   ├── llm-d-epp.yaml        # llm-d precise prefix-cache routing EPP (active)
│   ├── epp-config.yaml       # Vanilla EPP scorer weights (rollback target)
│   └── inference-objective.yaml # Request priority/criticality
├── k8s/
│   ├── app-deployment.yaml   # FastAPI app Deployment (per-cluster image tag)
│   ├── app-service.yaml      # FastAPI external LoadBalancer Service
│   └── rbac.yaml             # ServiceAccount + pod/deployment read RBAC
├── tests/
│   ├── test_loadtest_util.py # Unit tests (no cluster needed)
│   └── test_integration.py   # End-to-end tests against a live app
├── setup_infra.sh            # Provision GKE, CRDs, gateway, pools, EPP, baseline
├── deploy_app.sh             # Build, push, and deploy the FastAPI container
└── verify_setup.sh           # Post-deployment validation & test launcher
```

---

## 🚀 Getting Started

### 1. Configure
```bash
cp .env.example .env
```
Edit `.env` for your project, cluster name, zone, model, and GPU-sharing settings
(`REPLICAS`, `GPU_MEM_UTIL`, `GPU_SHARING_STRATEGY`, `GPU_MAX_SHARED`,
`GATEWAY_CLASS`). Use a **distinct `CLUSTER_NAME`** so you never touch an existing
cluster.

> **Prerequisite:** GPU sharing in a ComputeClass requires **GKE ≥
> 1.35.2-gke.1485000**. On some recent GKE versions, GPU-sharing nodes have
> advertised only `Allocatable nvidia.com/gpu: 1`; after the first GPU node is
> created, confirm it advertises `GPU_MAX_SHARED` allocatable GPUs (else lower
> `REPLICAS`).

### 2. Provision infrastructure
```bash
./setup_infra.sh
```
GPU-only. Portable/idempotent (macOS/Linux; `python3` for manifest substitution,
auto-downloads `helm`, pins the GAIE CRDs, creates a proxy-only subnet if missing).
It deploys the gateway, the `REPLICAS`-pod GPU deployment (one shared GPU), the
InferencePool + vanilla EPP (rollback), the **llm-d EPP** (active), and the
**`vllm-direct`** baseline Service.

Teardown for a clean rebuild (the shared proxy-only subnet is never deleted):
```bash
./setup_infra.sh --delete          # remove in-cluster resources (keep cluster)
./setup_infra.sh --delete-cluster  # the above, plus delete the GKE cluster
```

### 3. Build & deploy the app
```bash
./deploy_app.sh
```
The app image uses a **per-cluster tag** (`IMAGE_TAG`, defaults to `CLUSTER_NAME`)
so multiple clusters never clobber each other's image. Run this where Docker is
available.

### 4. Verify & test
```bash
./verify_setup.sh
```
Discovers the app's external IP, waits for pods, and runs `tests/`.

---

## 🔬 The Load-Test Comparison UI

The app serves a UI with two tabs.

### Load Test · llm-d vs round-robin (primary)
Set **concurrency**, **documents**, **queries/doc**, **max tokens**, then **Run
comparison**. The app fires `documents × queries/doc` requests per arm at the
chosen concurrency — first through `vllm-direct` (round-robin), then through the
llm-d gateway — each arm with its own fresh document nonce so neither benefits
from the other's cache. Results show two columns plus a headline delta banner:

| Metric | How it's measured |
|---|---|
| **Prefix cache hit rate** | cluster-wide Δ`vllm:prefix_cache_hits_total` / Δ`vllm:prefix_cache_queries_total` over the run (robust under concurrency) |
| **p50 / p95 TTFT** | from the first streamed token of each request |
| **Throughput (tok/s)** | total completion tokens / wall-clock |
| **Work per pod** | per-pod Δ`prefix_cache_queries_total` (llm-d concentrates a doc on one pod; round-robin spreads it) |

Endpoints: `POST /api/loadtest` (starts a run; inputs are clamped server-side),
`GET /api/loadtest/status` (progress + per-mode results + headline comparison).

### Playground · single request (secondary)
The original cold→warm single-request demo: preset/editable contexts, a real TTFT
graph, per-pod KV telemetry, and "New Run" (a fresh session prefix makes caches
cold without restarting pods). Good for narrating one request; not for proving
load.

The header shows a **GPU backend badge** and a **provisioning banner** driven by
`/api/status` (provisioning / re-provisioning on spot reclaim / degraded), gating
actions until the shared GPU node is Ready.

---

## How the routing works (llm-d precise prefix-cache routing)

The active Endpoint Picker is the **llm-d endpoint-picker** (`infra/llm-d-epp.yaml`).
vLLM publishes **KV-cache events** over ZMQ (`--kv-events-config`, port 5557 — see
`infra/gpu-deployment.yaml`); the EPP's `precise-prefix-cache-producer` subscribes
to every pod's socket and maintains a real, eviction-aware map of which prefix
blocks live on which pod. Its `prefix-cache-scorer` (weight 15 vs load scorers at
1) routes each request to the pod that *physically* holds the most of its prefix.
A `token-producer` tokenizes prompts via vLLM's `/v1/completions/render` (through
`vllm-model-svc`) so the EPP's block hashes match vLLM's.

> `BLOCK_SIZE` must match between vLLM `--block-size` and the EPP's
> `tokenProcessorConfig.blockSize` (GPU default 16).

**Two EPPs, instant rollback.** `setup_infra.sh` installs the vanilla
gateway-api-inference-extension EPP (rollback target, `infra/epp-config.yaml`) and
then flips the InferencePool's `endpointPickerRef` to the llm-d EPP. To roll back:
```bash
kubectl patch inferencepool vllm-server --type merge \
  -p '{"spec":{"endpointPickerRef":{"name":"vllm-server-epp"}}}'
```

> Note: this is the *precise prefix-cache routing* slice of llm-d, deployed as
> plain manifests (no Helm). P/D disaggregation (RDMA/NIXL) is out of scope.

---

## GPU sharing notes

- All `REPLICAS` pods share **one physical GPU** (`sharingStrategy: TIME_SHARING`,
  `maxSharedClientsPerGPU >= REPLICAS`). Each requests `nvidia.com/gpu: 1` and
  `--gpu-memory-utilization GPU_MEM_UTIL` (sum across pods < ~0.9).
- `GPU_SHARING_STRATEGY=MPS` gives concurrent kernels (better throughput) but
  needs `hostIPC: true` on the pod and gives no fault isolation.
- Throughput is capped at ~one GPU, *not* `REPLICAS`×. The comparison is about
  **efficiency per GPU-second**: round-robin wastes time-slices re-prefilling
  cache misses; llm-d spends them serving tokens.
- The `gpu-flex` ComputeClass falls back G4 (nvidia-rtx-pro-6000) → G2 (nvidia-l4),
  spot → on-demand.

**GPU latency:** the first request to a given pod/shape pays a one-time Triton JIT
compile (~20s); steady-state TTFT is tens of milliseconds.
