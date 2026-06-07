# Inference Gateway UI Demo Design: "The Dynamic Cache Engine"

## Objective
Create a highly intuitive, interactive user interface that allows presenters or end-users to experience the advanced routing and load-balancing capabilities of the GKE Inference Gateway. The demo provides real-time visual proof of how cache-aware routing decreases latency, spreads workloads, and manages cluster saturation using physical telemetry from CPU-based vLLM pods.

---

## User Interface Layout

### 1. The Control & Input Panel (Left Column)
*   **Hybrid Document Input**:
    *   **Preset Selection Cards**: Quick-click cards (*"Financial Report A,"* *"Medical Journal B,"* *"Engineering Spec C"*) containing dense, pre-tokenized sample texts for smooth stage presentations.
    *   **Interactive Drag-and-Drop Zone**: A dedicated upload area allowing users to drop custom text files to experiment dynamically.
*   **Prompt Input**: A standard text box for asking questions against the loaded context, with execution buttons.

### 2. Live Telemetry Dashboard (Right Column)
*   **Node Cards (Node 1 & Node 2)**: Visual representations of the physical backend CPU pods.
*   **Real-Time Meters**:
    *   **KV Cache Capacity Gauge**: Fills dynamically up to the physical 4 GB limit set in the container configuration.
    *   **Queue Depth Indicator**: Shows current active requests waiting for evaluation.
*   **Time-to-First-Token (TTFT) Graph**: Renders the response latency profile for each request.

### 3. Traffic Flow Visualization (Center Canvas)
*   **Color-Coded Flow Paths**: Animated tracing connecting the user input to the target node. Streams are color-coded by context (e.g., Context Alpha is Blue, Context Beta is Green) to make routing decisions instantly recognizable.

---

## The Demonstration Playbook

### Phase 1: Ingestion & Cache Affinity
1.  **Cold Start**: The presenter loads **Context Alpha** (Blue) and asks a question. The Gateway routes this to **Node 1**. Telemetry shows Node 1's cache meter rising. TTFT shows **~4.5 seconds** due to cold CPU prefill processing.
2.  **Affinity Payoff**: The presenter submits a follow-up question on Context Alpha. The Gateway identifies the active cache and routes it back to **Node 1**. TTFT drops immediately to **~0.3 seconds**.
3.  **Value Overlay**: *"93% TTFT reduction via Cache Affinity. Ingress routing bypassed prompt re-evaluation."*

### Phase 2: Load-Aware Partitioning
1.  **New Data**: The presenter loads a fresh document, **Context Beta** (Green), and submits a query.
2.  **Partitioning Payoff**: The Gateway evaluates cluster load. Seeing Node 1's cache is populated with Context Alpha, it intelligently routes Context Beta to **Node 2**. Telemetry shows Node 2 spinning up its compute to ingest the prefill.

### Phase 3: Saturation & Eviction Mechanics
1.  **Resource Exhaustion**: The presenter uploads additional heavy files (**Context Gamma**, **Context Delta**) until both Node 1 and Node 2's cache capacity gauges reach **100% saturation**.
2.  **Eviction Payoff**: The presenter uploads a new document, **Context Epsilon**. The Gateway routes it to Node 1. A visual alert flashes: *"Cache Saturation Reached. Applying LRU Eviction to Context Alpha on Node 1."* The cache meter reflects Context Alpha blocks being replaced.

### Phase 4: The Thrashing Proof
1.  **Cache Miss Proof**: The presenter asks a question about the evicted **Context Alpha**.
2.  **The Payoff**: The UI traces it to Node 1, but the graph registers a **Cache Miss**. TTFT spikes back to **~4.5 seconds**, proving physically that the eviction occurred and context had to be recomputed.

---

# Backend Architecture: Selectable CPU / GPU via Two Clusters

This section captures the deployment design for running the same demo on either a
**CPU** or a **GPU** backend. (The numbers in the playbook above are CPU-era; on GPU,
cold prefill drops to ~1s and warm hits to sub-second.)

## Principles
1. **Two separate clusters**, one per backend (`BACKEND=cpu|gpu`), each with its own
   gateway IP and app URL. There is **no single UI that chooses CPU vs GPU**.
2. The two stacks are **as identical as possible** — everything except the model-server
   deployment, the ComputeClass, and the KV block size is byte-for-byte the same.
3. **The UI states which backend it is** (CPU or GPU) prominently.
4. **Provisioning awareness**: if the model-server VM isn't provisioned yet, the UI shows
   a "provisioning…" state and keeps polling until it's Ready.
5. **Spot resilience**: if a spot VM is reclaimed (GPU or CPU), the UI shows it is
   re-provisioning and recovers automatically when capacity returns.

## What is identical across both clusters
Gateway, Inference-Extension CRDs, InferencePool, vanilla EPP, **llm-d precise EPP**,
HTTPRoute, InferenceObjectives, and the **app image** (same build). They select model
pods by a stable, hardware-neutral label (`app: vllm-server`) and proxy OpenAI calls —
they do not know or care about the hardware.

## What is parameterized by `BACKEND`
| Item | CPU | GPU |
|---|---|---|
| Model-server manifest | `infra/cpu-deployment.yaml` | `infra/gpu-deployment.yaml` |
| Image | `vllm/vllm-openai-cpu` | `vllm/vllm-openai` (`nvidia.com/gpu: 1`) |
| ComputeClass | `cpu-flex` | `gpu-flex` |
| `BLOCK_SIZE` (must match vLLM ↔ EPP `tokenProcessorConfig.blockSize`) | 128 | 16 |
| Cluster name | e.g. `cpu-flex-cluster` | e.g. `gpu-flex-cluster` |

Same model on both: **Qwen/Qwen2.5-1.5B-Instruct**.

## Compute: symmetric Custom Compute Classes (spot → on-demand, with family fallback)
Both clusters enable **Node Auto-Provisioning**; a `ComputeClass` defines a prioritized,
auto-provisioned node list. Model servers run on the ComputeClass (spot-preferred) pool;
the gateway/EPP/**app** run on a small **on-demand default pool** so the app stays up to
*report* provisioning even while the model node is being created or replaced.

```
gpu-flex:  G4 spot → G4 on-demand → G2 spot → G2 on-demand   (each tier carries a gpu block:
                                                              G4=nvidia-rtx-pro-6000, G2=nvidia-l4)
cpu-flex:  e2 → e4 → n4   (each spot → on-demand)
both:      nodePoolAutoCreation: enabled ; activeMigration (return to higher tier when freed)
```
*(G2 = NVIDIA L4; G4 = NVIDIA RTX PRO 6000 Blackwell — needs driver 580+ and GKE
≥1.34.1-gke.1279000 for NAP; full-G4 zonal availability isn't guaranteed, so it falls back to
G2/L4. CPU: e2 is the proven/available family (first); e4 is the newer option if it exists; n4
is a valid modern fallback so provisioning still succeeds if "e4" is unavailable.)*

If every tier is unavailable, model pods stay `Pending` (we **wait for capacity** — no
auto cross-backend fallback) and the UI shows "provisioning".

## UI: backend identity + provisioning state machine
The app reads `BACKEND` from env and exposes a `/api/status` endpoint computed from the
Kubernetes API (model Deployment `readyReplicas` + pod/node/events):

| Condition | `state` | UI |
|---|---|---|
| `readyReplicas ≥ 1` | `ready` | normal UI; backend badge "Backend: GPU / CPU" |
| desired ≥1, never-been-ready, pods `Pending`/`Unschedulable` | `provisioning` | banner "Provisioning {GPU/CPU} compute…"; Send disabled; keep polling |
| was ready, now `readyReplicas == 0` (spot reclaim) | `reprovisioning` | banner "Compute reclaimed (spot) — re-provisioning…"; keep polling |
| pods crash/image error | `degraded` | distinct error banner |

The UI polls `/api/status`, gates the controls on `state`, and **auto-recovers** to Ready.
"provisioning" deliberately covers every not-ready cause (node create → boot → driver →
image pull → model load).

## Operational model (matches "swap rarely; redeploy is fine")
- CPU cluster = always-available default; GPU cluster spun up on demand and torn down
  (`setup_infra.sh --delete-cluster`) when idle to save cost.
- Provision a backend: set `BACKEND` + `CLUSTER_NAME` in `.env`, then
  `./setup_infra.sh && ./deploy_app.sh` (run where helm + Docker are available).
- **Use a distinct `CLUSTER_NAME`** for these new clusters so the existing demo cluster is
  never touched.
