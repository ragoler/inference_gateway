# Inference Gateway on GKE with FastAPI Application

This repository automates the provisioning, configuration, and deployment of a Google Kubernetes Engine (GKE) cluster configured with the Kubernetes Gateway API Inference Extension. It routes LLM inference traffic to a CPU-based vLLM model server pool and provides a production-ready FastAPI consumer application.

---

## 🏗️ Architecture Overview

The system architecture is divided into three primary layers:

1. **Infrastructure Tier**: A dedicated GKE Cluster provisioned with the standard Kubernetes Gateway API.
2. **Inference Tier**:
   - **L7 Managed External Gateway** (`gke-l7-regional-external-managed`).
   - **Endpoint Picker (EPP) Extension**: Optimally balances requests across inference pools.
   - **vLLM CPU Model Servers**: Running high-performance serving for HuggingFace models (e.g., `Qwen/Qwen2.5-1.5B-Instruct`).
3. **Application Tier**:
   - A fully containerized Python FastAPI microservice running inside the cluster.
   - Exposes clean REST APIs (`/generate`) and seamlessly interacts with the backend Inference Gateway.

---

## 📁 Repository Structure

```
├── .env.example             # Template for configuration environment variables
├── app/
│   ├── Dockerfile           # Multi-stage containerization for the FastAPI service
│   ├── main.py              # FastAPI server handling prompt formatting & routing
│   └── requirements.txt     # Python runtime dependencies
├── infra/
│   ├── cpu-deployment.yaml  # vLLM model server (+ KV-cache event publishing over ZMQ)
│   ├── epp-config.yaml      # Vanilla EPP scorer weights (kept as rollback target)
│   ├── llm-d-epp.yaml       # llm-d precise prefix-cache routing EPP (active Endpoint Picker)
│   └── gateway.yaml         # External L7 Gateway definition
├── k8s/
│   ├── app-deployment.yaml  # FastAPI application Deployment manifest
│   └── app-service.yaml     # FastAPI external LoadBalancer Service
├── tests/
│   └── test_integration.py  # End-to-end automated testing suite
├── setup_infra.sh           # Script to provision GKE, CRDs, and inference pools
├── deploy_app.sh            # Script to build, push, and deploy the FastAPI container
└── verify_setup.sh          # Post-deployment validation & integration testing launcher
```

---

## 🚀 Getting Started

### 1. Environment Configuration
Create a `.env` file based on the provided template:
```bash
cp .env.example .env
```
Edit `.env` to configure your specific GCP project, cluster name, zones, machine types, and model options.

### 2. Provision the GKE Cluster & Gateway API
Execute the infrastructure setup script to provision your GKE cluster, enable Gateway API CRDs, and deploy the vLLM model servers:
```bash
./setup_infra.sh
```

### 3. Build and Deploy the FastAPI Microservice
Once the cluster and gateway are healthy, deploy the backend application:
```bash
./deploy_app.sh
```

### 4. Verify and Test
Automatically discover the external LoadBalancer IP and validate the entire end-to-end generative AI workflow:
```bash
./verify_setup.sh
```

---

## 🔬 The Live Demo UI (real telemetry, not simulated)

The FastAPI app serves an interactive UI that proves **KV-cache-aware routing** using real
signals scraped from the vLLM pods — no faked numbers.

**What's real:**
- **KV cache gauge** — scraped from `vllm:kv_cache_usage_perc` on each pod.
- **Which pod served a request** — derived from the per-pod delta of
  `vllm:prefix_cache_queries_total` around each request (`routing.served_by`).
- **Cache hit vs. cold prefill** — from the delta of `vllm:prefix_cache_hits_total`
  (`routing.cache_hit`, `routing.hit_ratio`).
- **Time-to-First-Token** — measured from the streamed first token (`/generate` proxies
  with `stream: true` and timestamps the first chunk).

**How the routing works (llm-d precise prefix-cache routing):** the active Endpoint Picker
is the **llm-d endpoint-picker** (`infra/llm-d-epp.yaml`). vLLM publishes **KV-cache events**
over ZMQ (`--kv-events-config`, port 5557 — see `infra/cpu-deployment.yaml`); the EPP's
`precise-prefix-cache-producer` subscribes to every pod's socket and maintains a real,
eviction-aware map of which prefix blocks live on which pod. Its `prefix-cache-scorer` then
routes each request to the pod that *physically* holds the most of its prefix — ground truth,
not a heuristic. A `token-producer` tokenizes prompts via vLLM's `/v1/completions/render`
(through `vllm-model-svc`) so the EPP's block hashes match vLLM's.

Result: sending the same long context twice routes the follow-up to the **same pod** and
returns a real cache hit (measured **~9.5s cold → ~0.6s warm TTFT**, affinity reliable).

**Two EPPs, instant rollback.** `setup_infra.sh` installs the vanilla
gateway-api-inference-extension EPP (Step 6) and tunes its weights (Step 7,
`infra/epp-config.yaml`, `prefix-cache-scorer` weight 10) as a heuristic fallback, then
deploys the llm-d EPP and **flips the InferencePool's `endpointPickerRef`** to it (Step 8).
To roll back to the heuristic EPP:
```bash
kubectl patch inferencepool vllm-cpu-server --type merge \
  -p '{"spec":{"endpointPickerRef":{"name":"vllm-cpu-server-epp"}}}'
```

> Note: this is the *precise prefix-cache routing* slice of llm-d, deployed as plain
> manifests (no Helm). P/D disaggregation (GPU + RDMA/NIXL) is out of scope for this CPU demo.

> ⚠️ **Prefix caching is block-aligned (block size = 128 tokens).** Prompts shorter than one
> block cache nothing and show no affinity. The UI's preset contexts are intentionally long
> (~200+ tokens) and are sent as the shared *prefix* of the request.

The `/generate` endpoint returns the OpenAI-style `choices`/`usage` plus `ttft_ms`,
`total_ms`, and a `routing` object (`served_by`, `cache_hit`, `hit_ratio`, and a
`confidence` of `exact`/`approximate`). It retries transient `503`/`429` (CPU prefill is
slow, so concurrent requests can briefly saturate the backends).

**Resetting between demo runs ("New Run"):** this vLLM build exposes no cache-reset
endpoint, and restarting pods costs minutes of CPU warmup. Instead, the UI prepends a
short **session tag** to every context. Because prefix caching hashes blocks in a chain
from the first token, bumping the tag makes every context a brand-new (cold) prefix
instantly — so you can re-run the cold→warm story without touching the pods. "New Run"
also rebaselines the per-pod hit-rate gauges and clears the TTFT graph and log.

> Note: per-pod hit-rate is shown **session-relative** (rebased on New Run). vLLM's raw
> counters and `cached_tokens` are cumulative and only truly zero on a pod restart.