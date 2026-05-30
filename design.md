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
