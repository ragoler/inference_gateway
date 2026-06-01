// Context Presets
// IMPORTANT: vLLM prefix caching is block-aligned (block_size = 128 tokens).
// These context documents are intentionally long (>128 tokens) so the shared
// prefix actually fills cache blocks and prefix-cache-aware routing engages.
const PRESETS = {
    Alpha: {
        name: "Context Alpha (Financial Report)",
        streamClass: "bg-sky-500/20 border-sky-500 text-sky-300",
        question: "What was the primary driver of revenue growth, and what did the board approve?",
        context: "FY2025 Annual Financial Report - Northwind Industries. Northwind Industries reported total revenue of 4.2 billion dollars for fiscal year 2025, a 24 percent increase over the prior year. The primary driver of this growth was the cloud infrastructure division, which expanded 41 percent year over year and now accounts for 58 percent of total revenue. Operating margin improved to 31 percent as the company realized economies of scale across its data center footprint. Free cash flow reached 410 million dollars. The board of directors approved a 500 million dollar share buyback program and raised fourth quarter guidance to 1.3 billion dollars in revenue. Headcount grew 8 percent to support continued expansion in the Asia Pacific and European markets. Key risks identified by management include foreign exchange volatility, supply chain constraints affecting server hardware, and increased competition in the enterprise segment. The audit committee confirmed there were no material weaknesses in internal controls."
    },
    Beta: {
        name: "Context Beta (Clinical Study)",
        streamClass: "bg-emerald-500/20 border-emerald-500 text-emerald-300",
        question: "List the most common side effects reported in the study.",
        context: "Clinical Study Report - Compound NX-117 Phase II Trial. This randomized double blind placebo controlled study evaluated the efficacy and safety of compound NX-117 in 480 adult patients with moderate to severe rheumatoid arthritis over a 24 week period. The primary endpoint, a 20 percent improvement in American College of Rheumatology criteria, was met by 62 percent of patients in the treatment arm versus 28 percent in the placebo arm. The most commonly reported side effects were mild headache, transient nausea, and injection site reactions, each occurring in fewer than 15 percent of participants. Three serious adverse events were recorded, two of which were determined to be unrelated to the study drug. No deaths occurred during the trial. Laboratory monitoring showed a small reversible elevation in liver enzymes in 6 percent of treated patients. The data safety monitoring board recommended continuation to a Phase III trial without modification to the dosing regimen."
    },
    Gamma: {
        name: "Context Gamma (Engineering Spec)",
        streamClass: "bg-fuchsia-500/20 border-fuchsia-500 text-fuchsia-300",
        question: "What is the maximum thermal dissipation and the coolant flow rate?",
        context: "Engineering Specification - Project Helios Thermal Management System. The Helios cooling subsystem is rated for a maximum thermal dissipation of 1200 watts per module across an operating temperature range of minus 20 to 70 degrees Celsius. The closed loop liquid coolant circulates at a nominal flow rate of 4.5 liters per minute, maintaining junction temperatures below 85 degrees under full load. Each module integrates dual redundant pumps with automatic failover within 50 milliseconds of a detected fault. The control board samples twelve thermistors at 10 hertz and adjusts pump speed using a proportional integral derivative loop. Material specifications call for 6061 aluminum cold plates with nickel plating to prevent galvanic corrosion. Mean time between failures is rated at 80000 hours. Compliance testing covers vibration to 5 g, thermal cycling over 500 cycles, and ingress protection rated to IP67. All units must pass a 48 hour burn in test before shipment to customers."
    }
};

// Neutral style for user-edited / custom context so it is clearly not a preset.
const CUSTOM_STYLE = "bg-slate-600/40 border-slate-400 text-slate-200";
const COLD_STYLE = "bg-slate-700 text-slate-300";

let currentContext = { key: "Cold", streamClass: COLD_STYLE };
let loadedPresetText = "";  // exact text of the last preset loaded; edits diverge -> Custom
let lastRouting = null;     // {served_by, hit, ctxKey} - persists across telemetry polls
let inflight = 0;
let ttftHistory = [];       // {ttft, hit, label}
let sessionNonce = "";      // prepended to contexts; bumping it makes caches cold
let sessionBaseline = {};   // {pod: {queries, hits}} captured at New Run for session hit-rate
let latestNodes = [];       // last telemetry snapshot

document.addEventListener("DOMContentLoaded", () => {
    setupDropZone();
    sessionNonce = makeNonce();
    document.getElementById("session-tag").innerText = `session: ${sessionNonce} (caches cold)`;
    pollTelemetry();
    setInterval(pollTelemetry, 2500);
    renderTtftGraph();
    updateCtxTokens();
});

function makeNonce() {
    return Math.random().toString(36).slice(2, 7);
}

// Start a fresh cold demo run without restarting pods:
//  - a new session prefix makes every context a brand-new prefix (cold + re-routed)
//  - per-pod hit-rate gauges reset to a session baseline
//  - TTFT graph, log, and routing highlight are cleared
function newRun() {
    sessionNonce = makeNonce();
    sessionBaseline = {};
    latestNodes.forEach(n => {
        sessionBaseline[n.name] = { queries: n.prefix_queries || 0, hits: n.prefix_hits || 0 };
    });
    ttftHistory = [];
    lastRouting = null;
    renderTtftGraph();
    document.getElementById("session-tag").innerText = `session: ${sessionNonce} (caches cold)`;
    document.getElementById("log-feed").innerHTML = "";
    const badge = document.getElementById("routing-stream");
    badge.className = "text-xs px-3 py-1 rounded-full bg-slate-900 border border-slate-700 text-slate-400 font-mono transition-all";
    badge.innerText = "Idle";
    pollTelemetry();
    logEvent(`New run ${sessionNonce}: contexts use a fresh prefix — next requests are cold and routing is re-decided.`);
}

function estTokens(text) {
    if (!text) return 0;
    return Math.round(text.trim().split(/\s+/).filter(Boolean).length * 1.3);
}

function updateCtxTokens() {
    const text = document.getElementById("context-input").value;
    const t = text.trim();
    const n = estTokens(text);
    const el = document.getElementById("ctx-tokens");
    el.innerText = `~${n} tokens`;
    el.className = "text-xs font-mono " + (n > 0 && n < 128 ? "text-amber-400" : "text-slate-500");

    // Re-classify the context as the user types.
    if (!t) {
        currentContext = { key: "Cold", streamClass: COLD_STYLE };
    } else if (currentContext.key === "Cold") {
        currentContext = { key: "Custom", streamClass: CUSTOM_STYLE };
    } else if (currentContext.key !== "Custom" && t !== loadedPresetText.trim()) {
        // Edited away from the preset it was loaded from.
        currentContext = { key: "Custom", streamClass: CUSTOM_STYLE };
    }
    updateCtxLabel();
}

// Render the small badge showing which context is active (Alpha/Beta/Gamma/Custom).
function updateCtxLabel() {
    const el = document.getElementById("ctx-label");
    if (currentContext.key === "Cold") { el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    el.className = `text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full border ${currentContext.streamClass}`;
    el.innerText = currentContext.key === "Custom" ? "Custom (edited)" : currentContext.key;
}

// Load preset data into the editable context box
function loadPreset(key) {
    const preset = PRESETS[key];
    currentContext = { key: key, streamClass: preset.streamClass };
    loadedPresetText = preset.context;
    document.getElementById("context-input").value = preset.context;
    document.getElementById("prompt-input").value = preset.question;
    updateCtxTokens();
    logEvent(`Loaded ${preset.name} into the context box (~${estTokens(preset.context)} tokens). Edit it freely.`);
}

// File Drag and Drop
function setupDropZone() {
    const dropZone = document.getElementById("drop-zone");
    const fileInput = document.getElementById("file-input");
    dropZone.addEventListener("click", () => fileInput.click());
    dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("border-sky-400"); });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("border-sky-400"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("border-sky-400");
        if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener("change", () => { if (fileInput.files.length) handleFile(fileInput.files[0]); });
}

function handleFile(file) {
    const reader = new FileReader();
    reader.onload = (e) => {
        const text = (e.target.result || "").trim();
        currentContext = { key: "Custom", streamClass: CUSTOM_STYLE };
        loadedPresetText = "";
        document.getElementById("context-input").value = text;
        document.getElementById("prompt-input").value = "Summarize the attached document.";
        updateCtxTokens();
        const n = estTokens(text);
        if (n < 128) logEvent(`Loaded ${file.name} (~${n} tokens). Under 128 tokens, prefix caching will not engage.`, true);
        else logEvent(`Loaded ${file.name} into the context box (~${n} tokens).`);
    };
    reader.readAsText(file);
}

// Telemetry polling
async function pollTelemetry() {
    try {
        const response = await fetch("/api/telemetry");
        const data = await response.json();
        if (data.nodes) renderNodes(data.nodes);
    } catch (err) {
        console.error("Failed to poll telemetry", err);
    }
}

// Render backend pod cards (preserves the persistent "last routed" highlight)
function renderNodes(nodes) {
    latestNodes = nodes;
    const container = document.getElementById("nodes-grid");
    container.innerHTML = "";

    nodes.forEach((node, idx) => {
        // Session-relative hit rate: subtract the baseline captured at New Run.
        const base = sessionBaseline[node.name];
        let sQ = node.prefix_queries || 0;
        let sH = node.prefix_hits || 0;
        if (base) { sQ -= base.queries; sH -= base.hits; }
        const hitPct = sQ > 0 ? Math.round((sH / sQ) * 100) : 0;
        const running = node.running || 0;
        const cachedTokens = node.cached_tokens || 0;
        const isLastRouted = lastRouting && lastRouting.served_by === node.name;
        const routedHit = isLastRouted && lastRouting.hit;

        const ringClass = isLastRouted
            ? (routedHit ? "border-sky-400 shadow-lg shadow-sky-500/20" : "border-amber-400 shadow-lg shadow-amber-500/20")
            : "border-slate-700";

        const card = document.createElement("div");
        card.className = `bg-slate-800 rounded-xl p-5 border ${ringClass} flex flex-col space-y-4 relative overflow-hidden transition-all duration-300`;
        card.id = `node-card-${node.name}`;

        const routedRibbon = isLastRouted
            ? `<span class="absolute top-0 right-0 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider rounded-bl ${routedHit ? 'bg-sky-500 text-white' : 'bg-amber-500 text-white'}">&#9664; Last routed${routedHit ? ' &middot; HIT' : ' &middot; cold'}</span>`
            : "";

        const activeBadge = running > 0
            ? `<span class="flex items-center text-[10px] font-bold uppercase tracking-wider text-emerald-400"><span class="w-2 h-2 rounded-full bg-emerald-400 mr-1 animate-pulse"></span>Active</span>`
            : `<span class="text-[10px] font-mono text-slate-600">idle</span>`;

        card.innerHTML = `
            ${routedRibbon}
            <div class="flex justify-between items-start">
                <div>
                    <span class="text-xs font-mono text-slate-500 uppercase">Worker Pod ${idx + 1}</span>
                    <h3 class="text-sm font-bold text-white truncate w-44 font-mono mt-0.5">${node.name}</h3>
                </div>
                <div class="flex flex-col items-end space-y-1">
                    ${activeBadge}
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${node.queue_length > 0 ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30' : 'bg-slate-900 text-slate-400'}">Queue: ${node.queue_length}</span>
                </div>
            </div>

            <!-- Prefix Cache Hit Rate (real, session-relative) -->
            <div class="space-y-1.5">
                <div class="flex justify-between text-xs">
                    <span class="text-slate-400">Prefix Cache Hit Rate <span class="text-slate-600">(session)</span></span>
                    <span class="font-mono font-medium text-sky-400">${hitPct}%</span>
                </div>
                <div class="w-full bg-slate-900 h-2 rounded-full overflow-hidden p-0.5 border border-slate-800">
                    <div class="h-full rounded-full bg-sky-400 transition-all duration-500" style="width: ${hitPct}%"></div>
                </div>
            </div>

            <!-- Real serving stats -->
            <div class="pt-2 border-t border-slate-700/50 grid grid-cols-2 gap-y-2 text-xs">
                <div class="flex flex-col">
                    <span class="text-slate-500">Cached tokens</span>
                    <span class="font-mono text-slate-300">${cachedTokens.toLocaleString()}</span>
                </div>
                <div class="flex flex-col">
                    <span class="text-slate-500">Running</span>
                    <span class="font-mono ${running > 0 ? 'text-emerald-400' : 'text-slate-400'}">${running} req</span>
                </div>
                <div class="flex flex-col col-span-2">
                    <span class="text-slate-500">KV Cache capacity</span>
                    <span class="font-mono text-slate-400">4.0 GB &middot; 1170 blocks &middot; 128 tok/block</span>
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

// Execute Prompt Request (concurrent-friendly)
async function executeRequest() {
    const question = document.getElementById("prompt-input").value.trim();
    const context = document.getElementById("context-input").value.trim();
    if (!question) { alert("Please enter a question."); return; }

    // Build the actual prompt: context becomes the shared PREFIX.
    let prompt = question;
    let ctxKey = "Cold";
    let streamClass = "bg-slate-700 text-slate-300";
    if (context) {
        // Session tag leads the prefix; bumping it (New Run) makes the cache cold.
        prompt = `Session ${sessionNonce}. ${context}\n\nQuestion: ${question}\nAnswer:`;
        ctxKey = currentContext.key || "Custom";
        streamClass = currentContext.streamClass;
    }

    inflight += 1;
    updateInflight();
    const streamBadge = document.getElementById("routing-stream");
    streamBadge.className = `text-xs px-3 py-1 rounded-full font-mono transition-all ${streamClass}`;
    streamBadge.innerText = `Routing: ${ctxKey}...`;

    try {
        const response = await fetch("/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: prompt, max_tokens: 40 })
        });

        if (response.ok) {
            const data = await response.json();
            const routing = data.routing || {};
            const ttft = data.ttft_ms;
            const total = data.total_ms;
            const hit = routing.cache_hit === true;
            const servedBy = routing.served_by;
            const approx = routing.confidence === "approximate";
            const podShort = servedBy ? servedBy.replace(/^vllm-cpu-server-[^-]+-/, "") : "?";
            const podLabel = approx ? `${podShort} (approx)` : podShort;
            const ttftStr = (ttft != null) ? `${(ttft / 1000).toFixed(2)}s` : "n/a";

            let stateText;
            if (!routing.available) stateText = "routing signal unavailable";
            else if (hit) stateText = `KV CACHE HIT (${Math.round(routing.hit_ratio * 100)}% of blocks)`;
            else stateText = "COLD PREFILL (cache miss)";

            logEvent(`[${ctxKey}] TTFT ${ttftStr} | total ${(total / 1000).toFixed(2)}s | ${stateText} | pod ${podLabel}`, routing.available && !hit);
            if (approx) logEvent(`  ↳ attribution approximate (requests overlapped). Send one at a time for exact pod.`, true);

            if (ttft != null) {
                ttftHistory.push({ ttft: ttft, hit: hit, label: ctxKey });
                if (ttftHistory.length > 12) ttftHistory.shift();
                renderTtftGraph();
            }

            // Persist routing so the highlight survives telemetry re-renders.
            if (servedBy) {
                lastRouting = { served_by: servedBy, hit: hit, ctxKey: ctxKey };
                pollTelemetry();   // immediate re-render with the new highlight
                setTimeout(() => {
                    const card = document.getElementById(`node-card-${servedBy}`);
                    if (card) { card.classList.add("route-pulse"); setTimeout(() => card.classList.remove("route-pulse"), 3000); }
                }, 120);
            }

            streamBadge.className = `text-xs px-3 py-1 rounded-full font-mono transition-all ${hit ? 'bg-sky-500/20 border border-sky-500 text-sky-300' : 'bg-amber-500/20 border border-amber-500 text-amber-300'}`;
            streamBadge.innerText = hit ? `Cache Hit → ${podLabel}` : `Cold Prefill → ${podLabel}`;
        } else {
            const err = await response.json().catch(() => ({}));
            logEvent(`Request failed (HTTP ${response.status}): ${err.detail || ""}`, true);
        }
    } catch (err) {
        logEvent(`Gateway execution error: ${err.message}`, true);
    } finally {
        inflight -= 1;
        updateInflight();
        if (inflight === 0) {
            setTimeout(() => {
                if (inflight === 0) {
                    streamBadge.className = "text-xs px-3 py-1 rounded-full bg-slate-900 border border-slate-700 text-slate-400 font-mono transition-all";
                    streamBadge.innerText = "Idle";
                }
            }, 4000);
        }
    }
}

function updateInflight() {
    const el = document.getElementById("inflight");
    el.innerText = inflight === 0 ? "idle" : `${inflight} in flight`;
    el.className = "text-xs font-mono whitespace-nowrap " + (inflight > 0 ? "text-sky-400" : "text-slate-500");
}

// Render the Time-to-First-Token bar graph from real measurements.
function renderTtftGraph() {
    const graph = document.getElementById("ttft-graph");
    if (!graph) return;
    graph.innerHTML = "";
    if (!ttftHistory.length) {
        graph.innerHTML = `<div class="text-xs text-slate-600 self-center w-full text-center">No requests yet. Load a context and send a request to plot real TTFT.</div>`;
        return;
    }
    const maxTtft = Math.max(...ttftHistory.map(h => h.ttft), 1000);
    ttftHistory.forEach((h) => {
        const heightPct = Math.max((h.ttft / maxTtft) * 100, 4);
        const barColor = h.hit ? "bg-sky-400" : "bg-amber-400";
        const wrap = document.createElement("div");
        wrap.className = "flex-1 flex flex-col items-center justify-end h-full";
        wrap.innerHTML = `
            <span class="text-[10px] font-mono text-slate-400 mb-1">${(h.ttft / 1000).toFixed(1)}s</span>
            <div class="w-full ${barColor} rounded-t transition-all duration-500" style="height: ${heightPct}%"></div>
            <span class="text-[9px] font-mono text-slate-600 mt-1 truncate w-full text-center">${h.label}</span>
        `;
        graph.appendChild(wrap);
    });
}

function logEvent(msg, isError = false) {
    const feed = document.getElementById("log-feed");
    const item = document.createElement("div");
    const timestamp = new Date().toLocaleTimeString();
    item.className = isError ? "text-amber-400" : "text-slate-300";
    item.innerHTML = `<span class="text-slate-600">[${timestamp}]</span> ${msg}`;
    feed.appendChild(item);
    feed.scrollTop = feed.scrollHeight;
}
