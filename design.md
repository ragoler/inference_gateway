# Inference Gateway Demo Design: llm-d Load-Test Comparison

## Objective
Prove the value of **llm-d cache-aware routing** on GPU under realistic load by
running the **same concurrent workload two ways** and comparing them side by side:

- **With llm-d** — requests go through the GKE Inference Gateway whose Endpoint
  Picker (EPP) does *precise prefix-cache routing*: it consumes vLLM's KV-cache
  events and routes each request to the pod that physically holds its prefix.
- **Without llm-d** — requests go through a plain Kubernetes `Service`
  (`vllm-direct`), i.e. cache-blind round-robin across the same pods.

A single request on a fast GPU shows little (a cold prefill is ~1s, a warm hit is
sub-second — the difference is easy to miss). **Under load it is obvious:** with
cache-blind routing, repeated documents scatter across pods and get re-prefilled
on every pod; with llm-d, each document's repeats land on the pod that already
holds it, so the shared GPU wastes far less compute and tail latency/throughput
improve.

> This replaces the earlier single-request CPU "cache magic" playground, which is
> kept as a **secondary tab** for narrating one request at a time.

---

## Why a shared GPU (4 pods on 1 GPU)
Getting 4+ separate GPU machines is hard, and the 1.5B model is tiny, so all
model-server replicas **share one physical GPU** via GKE GPU sharing
(`sharingStrategy: TIME_SHARING`, `maxSharedClientsPerGPU >= REPLICAS`). Each pod
requests `nvidia.com/gpu: 1` and `--gpu-memory-utilization 0.20` (4 × 0.20 = 0.80
of 96 GB on an RTX PRO 6000).

**What this means for the story:** total throughput is capped at ~one GPU, *not*
4×. So the comparison is about **efficiency per GPU-second**: round-robin burns
time-slices re-prefilling cache misses; llm-d spends them serving useful tokens.
On a shared GPU, wasted prefill is *directly* lost throughput — which makes the
gap clean and honest rather than inflated by extra hardware.

**Prerequisites / risks (verify on the cluster):**
- ComputeClass GPU sharing requires **GKE ≥ 1.35.2-gke.1485000**.
- Some recent GKE versions regressed so that GPU-sharing nodes advertise only
  `Allocatable nvidia.com/gpu: 1`. Confirm the node advertises `GPU_MAX_SHARED`
  allocatable GPUs after the first node is created; if not, lower `REPLICAS` or
  use 2 GPUs.
- Full-G4 availability is per-zone; the ComputeClass falls back G4 → G2 (L4).

---

## Apples-to-apples
Both arms must pay the same network/serving cost so only routing differs:
- The llm-d gateway is **internal** (`gke-l7-rilb`), so the app reaches it over an
  in-cluster IP — no external hairpin penalty versus the in-cluster `vllm-direct`
  ClusterIP.
- Both arms hit the **same pods** and the **same model**.
- Each arm uses its **own fresh document nonce**, so neither benefits from the
  other's warm cache.

---

## What is measured (and how it stays honest under concurrency)
The app's load-test runner (`POST /api/loadtest`, `GET /api/loadtest/status`)
fires `num_docs × queries_per_doc` requests per arm at a chosen `concurrency`,
streaming each so it can time the first token. Per arm it reports:

| Metric | Source |
|---|---|
| **Prefix cache hit rate** | cluster-wide Δ`vllm:prefix_cache_hits_total` / Δ`vllm:prefix_cache_queries_total` summed across pods over the run (robust under concurrency; per-request delta attribution is racy) |
| **p50 / p95 / mean TTFT** | measured from the first streamed token of every request |
| **Throughput (tok/s)** | total completion tokens / wall-clock |
| **Requests ok/failed** | per-request status |
| **Work per pod** | per-pod Δ`prefix_cache_queries_total` — shows llm-d concentrating a document on one pod vs round-robin spreading it |

The headline banner shows the deltas: **p95 TTFT speedup**, **hit-rate gain**,
**throughput ratio** (llm-d ÷ round-robin).

---

## UI
Two tabs:
1. **Load Test · llm-d vs round-robin** (primary): controls (concurrency,
   documents, queries/doc, max tokens) + a **Run comparison** button; a headline
   delta banner; two result columns (With llm-d / Without llm-d) each showing hit
   rate, p50/p95 TTFT, throughput, requests, and a per-pod work bar chart. While
   running it polls `/api/loadtest/status` and shows which arm is in flight.
2. **Playground · single request** (secondary): the original cold→warm
   single-request demo (presets, editable context, real TTFT graph, per-pod KV
   telemetry, New Run). Useful for narrating one request, not for proving load.

The header shows a **backend badge** (GPU) and a **provisioning banner**: while
the shared GPU node is being created (or a spot node was reclaimed) the UI shows
"provisioning / re-provisioning…" via `/api/status` and disables actions until
Ready.

---

## Architecture (GPU-only)

| Layer | Component |
|---|---|
| Gateway | `${GATEWAY_NAME}`, class `gke-l7-rilb` (internal) — `infra/gateway.yaml` |
| Routing (with llm-d) | InferencePool + **llm-d precise prefix-cache EPP** — `infra/llm-d-epp.yaml` (vanilla EPP kept as rollback, `infra/epp-config.yaml`) |
| Baseline (without llm-d) | plain ClusterIP `vllm-direct` — `infra/vllm-direct.yaml` |
| Model servers | `REPLICAS` vLLM pods **sharing one GPU** — `infra/gpu-deployment.yaml` |
| Compute | `gpu-flex` ComputeClass: G4 spot→on-demand→G2 spot→on-demand, each with `gpuSharing` — `infra/computeclass-gpu.yaml` |
| App | FastAPI + UI (load-test runner, telemetry, status) — `app/`, `k8s/` |

`BLOCK_SIZE` must match between vLLM `--block-size` and the EPP's
`tokenProcessorConfig.blockSize` (GPU default 16).

## Parameters (`.env`)
| Var | Default | Purpose |
|---|---|---|
| `REPLICAS` | 4 | vLLM pods sharing one GPU |
| `GPU_MEM_UTIL` | 0.20 | per-pod `--gpu-memory-utilization` (sum < ~0.9) |
| `GPU_SHARING_STRATEGY` | `TIME_SHARING` | or `MPS` (MPS needs `hostIPC:true`) |
| `GPU_MAX_SHARED` | 4 | ComputeClass `maxSharedClientsPerGPU` (≥ `REPLICAS`) |
| `GATEWAY_CLASS` | `gke-l7-rilb` | internal for apples-to-apples; or external-managed |
| `BLOCK_SIZE` | 16 | KV block size (vLLM ↔ EPP) |
| `MODEL_NAME` | `Qwen/Qwen2.5-1.5B-Instruct` | configurable; start small |
| `IMAGE_TAG` | `${CLUSTER_NAME}` | per-cluster app image tag (no shared `:latest`) |

## Operational model
- Provision: set `.env`, then `./setup_infra.sh && ./deploy_app.sh` (run
  `deploy_app.sh` where Docker is available). Teardown: `./setup_infra.sh --delete`
  (keeps the cluster) or `--delete-cluster`. The shared proxy-only subnet is never
  deleted.
- Use a **distinct `CLUSTER_NAME`** so existing clusters are never touched.
