// Context Presets
// IMPORTANT: vLLM prefix caching is block-aligned (block_size = 128 tokens).
// These context documents are intentionally long (>128 tokens) so the shared
// prefix actually fills cache blocks and prefix-cache-aware routing engages.
const PRESETS = {
    Alpha: {
        name: "Context Alpha (Financial Report)",
        streamClass: "bg-sky-500/20 border-sky-500 text-sky-300",
        question: "What was the primary driver of revenue growth, and what did the board approve?",
        context: "FY2025 Annual Financial Report - Northwind Industries. Northwind Industries reported total revenue of 4.2 billion dollars for fiscal year 2025, a 24 percent increase over the prior year. The primary driver of this growth was the cloud infrastructure division, which expanded 41 percent year over year and now accounts for 58 percent of total revenue. The platform services segment grew 19 percent, while the legacy on premise licensing segment declined 6 percent as customers continued migrating to the cloud. Operating margin improved to 31 percent as the company realized economies of scale across its data center footprint and renegotiated long term power and bandwidth contracts. Gross margin was 68 percent. Free cash flow reached 410 million dollars and the company ended the year with 2.1 billion dollars in cash and short term investments and 900 million dollars of long term debt. By region, the Americas contributed 54 percent of revenue, Europe Middle East and Africa 28 percent, and Asia Pacific 18 percent, with Asia Pacific growing fastest at 33 percent. The board of directors approved a 500 million dollar share buyback program and raised fourth quarter guidance to 1.3 billion dollars in revenue. The board also authorized 1.4 billion dollars of capital expenditure for fiscal 2026, primarily for new data center capacity and custom inference accelerators. Headcount grew 8 percent to support continued expansion in the Asia Pacific and European markets, with research and development spending rising to 16 percent of revenue. Deferred revenue, a forward indicator of bookings, increased 27 percent to 1.1 billion dollars. Net revenue retention among enterprise customers was 121 percent. Key risks identified by management include foreign exchange volatility, supply chain constraints affecting server hardware and accelerator availability, customer concentration among the ten largest accounts, and increased competition in the enterprise segment from hyperscale providers. The audit committee confirmed there were no material weaknesses in internal controls and reappointed the external auditor for the coming year."
    },
    Beta: {
        name: "Context Beta (Clinical Study)",
        streamClass: "bg-emerald-500/20 border-emerald-500 text-emerald-300",
        question: "List the most common side effects reported in the study.",
        context: "Clinical Study Report - Compound NX-117 Phase II Trial. This randomized double blind placebo controlled multicenter study evaluated the efficacy and safety of compound NX-117 in 480 adult patients with moderate to severe rheumatoid arthritis over a 24 week treatment period followed by an 8 week safety follow up. Patients were randomized one to one to receive either NX-117 administered subcutaneously every two weeks or matching placebo, stratified by baseline disease activity and prior biologic exposure. The mean patient age was 54 years, 71 percent were female, and the mean disease duration was 9 years. The primary endpoint, a 20 percent improvement in American College of Rheumatology criteria at week 24, was met by 62 percent of patients in the treatment arm versus 28 percent in the placebo arm, a statistically significant difference with a p value below 0.001. Key secondary endpoints, including a 50 percent and a 70 percent improvement in the same criteria, and change in the Disease Activity Score 28, were all met in favor of the treatment arm. Improvement was observed as early as week 4 and was sustained through week 24. The most commonly reported side effects were mild headache, transient nausea, and injection site reactions, each occurring in fewer than 15 percent of participants and generally resolving without intervention. Upper respiratory tract infections occurred in 11 percent of treated patients versus 9 percent on placebo. Three serious adverse events were recorded, two of which were determined to be unrelated to the study drug by the blinded adjudication committee. No deaths occurred during the trial. Laboratory monitoring showed a small reversible elevation in liver enzymes in 6 percent of treated patients, none exceeding three times the upper limit of normal, and a transient decrease in neutrophil count in 4 percent. Pharmacokinetic analysis confirmed dose proportional exposure and a terminal half life of approximately 11 days supporting the every two week regimen. The data safety monitoring board reviewed all unblinded safety data and recommended continuation to a Phase III trial without modification to the dosing regimen."
    },
    Gamma: {
        name: "Context Gamma (Engineering Spec)",
        streamClass: "bg-fuchsia-500/20 border-fuchsia-500 text-fuchsia-300",
        question: "What is the maximum thermal dissipation and the coolant flow rate?",
        context: "Engineering Specification - Project Helios Thermal Management System. The Helios cooling subsystem is rated for a maximum thermal dissipation of 1200 watts per module across an operating temperature range of minus 20 to 70 degrees Celsius. The closed loop liquid coolant circulates at a nominal flow rate of 4.5 liters per minute, maintaining junction temperatures below 85 degrees under full load. The coolant is a propylene glycol and water mixture at a 30 to 70 ratio, with a reservoir capacity of 1.2 liters and a maximum system pressure of 2.5 bar. Each module integrates dual redundant pumps with automatic failover within 50 milliseconds of a detected fault, and a brushless fan array rated at 220 cubic feet per minute provides secondary air cooling for ancillary components. The control board samples twelve thermistors at 10 hertz and adjusts pump speed using a proportional integral derivative loop tuned for a settling time under two seconds and an overshoot below five percent. Material specifications call for 6061 aluminum cold plates with nickel plating to prevent galvanic corrosion, EPDM gaskets rated to 120 degrees Celsius, and stainless steel quick disconnect fittings with automatic shutoff. The power subsystem accepts 48 volts direct current with a tolerance of plus or minus 10 percent and draws no more than 60 watts at peak pump and fan load. Firmware implements a watchdog timer, over temperature shutdown at 95 degrees, leak detection through a conductive sense wire, and a CAN bus telemetry interface reporting at 1 hertz. Mean time between failures is rated at 80000 hours under nominal conditions. Compliance testing covers vibration to 5 g across three axes, mechanical shock to 30 g, thermal cycling over 500 cycles from minus 40 to 85 degrees, electromagnetic compatibility per the relevant class B limits, and ingress protection rated to IP67. Each unit ships with a calibration certificate, and all units must pass a 48 hour burn in test at elevated temperature before shipment to customers. Recommended maintenance is an annual coolant replacement and a pump inspection every 16000 hours of operation."
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
let prevEvictions = {};     // pod -> last eviction count, to detect new evictions
let backendReady = true;    // gated by /api/status; false while compute is provisioning

document.addEventListener("DOMContentLoaded", () => {
    setupDropZone();
    sessionNonce = makeNonce();
    document.getElementById("session-tag").innerText = `session: ${sessionNonce} (caches cold)`;
    pollTelemetry();
    setInterval(pollTelemetry, 2500);
    pollStatus();
    setInterval(pollStatus, 2500);
    renderTtftGraph();
    updateCtxTokens();
    updatePatternHelp();
    pollLoadTest();   // restore any in-progress / last comparison on load
});

// Inline explanation under the request-order dropdown; updates as you choose.
const PATTERN_HELP = {
    grouped: "Grouped: a document's queries are sent back-to-back. Balanced baseline.",
    shuffle: "Shuffled: all requests randomized into realistic mixed traffic. Round-robin scatters the cache most here, so llm-d's lead is widest under load (same random order for both arms).",
    stagger: "Staggered: each document is primed once (cold), then its repeats are sent — guaranteed warm. Cleanest cold→warm story and the best-case llm-d hit rate. Recommended for a clear demo.",
    interleave: "Interleaved: round-robin across documents (doc0·q1, doc1·q1, …). Steady, even load across all docs.",
};

function updatePatternHelp() {
    const sel = document.getElementById("lt-pattern");
    const el = document.getElementById("lt-pattern-help");
    if (!sel || !el) return;
    el.innerText = PATTERN_HELP[sel.value] || "";
}

// --------------------------------------------------------------------------
// Tabs: Load Test (primary) vs Playground (secondary single-request demo)
// --------------------------------------------------------------------------
function switchTab(name) {
    const tabs = ["loadtest", "playground"];
    tabs.forEach((t) => {
        const pane = document.getElementById(`tab-${t}`);
        const btn = document.getElementById(`tabbtn-${t}`);
        const active = t === name;
        pane.classList.toggle("hidden", !active);
        btn.className = "px-4 py-3 text-sm font-medium border-b-2 " +
            (active ? "border-sky-400 text-sky-300" : "border-transparent text-slate-400 hover:text-slate-200");
    });
}

// --------------------------------------------------------------------------
// Load-test comparison: WITH llm-d (gateway) vs WITHOUT (round-robin Service)
// --------------------------------------------------------------------------
let loadtestPolling = false;

async function runLoadTest() {
    if (!backendReady) { setLtProgress("Backend is still provisioning — wait until it is Ready.", true); return; }
    const body = {
        concurrency: parseInt(document.getElementById("lt-concurrency").value, 10) || 8,
        num_docs: parseInt(document.getElementById("lt-numdocs").value, 10) || 8,
        queries_per_doc: parseInt(document.getElementById("lt-queries").value, 10) || 4,
        max_tokens: parseInt(document.getElementById("lt-maxtokens").value, 10) || 16,
        pattern: document.getElementById("lt-pattern").value || "grouped",
    };
    setLtRunning(true);
    setLtProgress("Starting…");
    try {
        const resp = await fetch("/api/loadtest", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            setLtProgress(`Could not start: ${err.detail || resp.status}`, true);
            setLtRunning(false);
            return;
        }
        const d = await resp.json();
        setLtProgress(`Running ${d.requests_per_mode} requests per mode (direct, then llm-d)…`);
        if (!loadtestPolling) pollLoadTest();
    } catch (err) {
        setLtProgress(`Error: ${err.message}`, true);
        setLtRunning(false);
    }
}

async function pollLoadTest() {
    loadtestPolling = true;
    try {
        const d = await (await fetch("/api/loadtest/status")).json();
        renderLoadTest(d);
        if (d.running) {
            setLtRunning(true);
            const pat = (d.params && d.params.pattern) ? d.params.pattern : "";
            const tag = pat ? `[${pat}] ` : "";
            const phase = d.phase === "running:direct" ? `Running ${tag}— round-robin (without llm-d)…`
                        : d.phase === "running:llmd" ? `Running ${tag}— llm-d (cache-aware)…` : "Running…";
            setLtProgress(phase);
            setTimeout(pollLoadTest, 800);
        } else {
            loadtestPolling = false;
            setLtRunning(false);
            const donePat = (d.params && d.params.pattern) ? ` (${d.params.pattern} order)` : "";
            if (d.phase === "done") setLtProgress(`Comparison complete${donePat}.`);
            else if (d.phase === "error") setLtProgress(`Failed: ${d.error || "unknown error"}`, true);
        }
    } catch (err) {
        loadtestPolling = false;
        setLtRunning(false);
        setLtProgress(`Status error: ${err.message}`, true);
    }
}

function setLtRunning(running) {
    const btn = document.getElementById("loadtest-run");
    btn.disabled = running;
    btn.classList.toggle("opacity-50", running);
    btn.classList.toggle("cursor-not-allowed", running);
    btn.innerText = running ? "Running…" : "Run comparison";
}

function setLtProgress(msg, isError = false) {
    const el = document.getElementById("lt-progress");
    el.innerText = msg;
    el.className = "mt-4 text-sm font-mono " + (isError ? "text-amber-400" : "text-slate-400");
}

function renderLoadTest(d) {
    document.getElementById("lt-col-llmd").innerHTML =
        ltColumn("With llm-d", "cache-aware routing", d.llmd, "sky", d.running && d.phase === "running:llmd");
    document.getElementById("lt-col-direct").innerHTML =
        ltColumn("Without llm-d", "round-robin Service", d.direct, "slate", d.running && d.phase === "running:direct");
    renderLtHeadline(d);
}

// Render one result column. metrics may be null (not run yet / in progress).
function ltColumn(title, subtitle, m, accent, active) {
    const accentText = accent === "sky" ? "text-sky-400" : "text-slate-300";
    const accentBar = accent === "sky" ? "bg-sky-400" : "bg-slate-400";
    if (!m) {
        const state = active
            ? `<span class="text-amber-400 animate-pulse">running…</span>`
            : `<span class="text-slate-600">not run yet</span>`;
        return `<div class="bg-slate-800 rounded-xl p-6 border border-slate-700 h-full">
            <h3 class="text-lg font-semibold ${accentText}">${title}</h3>
            <p class="text-xs text-slate-500 mb-4">${subtitle}</p>
            <div class="text-sm font-mono">${state}</div></div>`;
    }
    const hitPct = Math.round((m.hit_rate || 0) * 100);
    const spread = m.pod_spread || {};
    const maxSpread = Math.max(1, ...Object.values(spread));
    const spreadRows = Object.keys(spread).sort().map((name, i) => {
        const v = spread[name] || 0;
        const w = Math.round((v / maxSpread) * 100);
        const short = name.replace(/^vllm-server-/, "");
        return `<div class="flex items-center space-x-2 text-[11px] font-mono">
            <span class="text-slate-500 w-24 truncate" title="${name}">pod ${i + 1}</span>
            <div class="flex-1 bg-slate-900 h-2 rounded-full overflow-hidden"><div class="h-full ${accentBar}" style="width:${w}%"></div></div>
            <span class="text-slate-400 w-20 text-right">${v.toLocaleString()} q</span></div>`;
    }).join("");

    return `<div class="bg-slate-800 rounded-xl p-6 border border-slate-700 h-full flex flex-col">
        <h3 class="text-lg font-semibold ${accentText}">${title}</h3>
        <p class="text-xs text-slate-500 mb-4">${subtitle}</p>

        <div class="text-center mb-4">
            <div class="text-5xl font-bold ${accentText}">${hitPct}%</div>
            <div class="text-xs text-slate-500 uppercase tracking-wider">prefix cache hit rate</div>
        </div>

        <div class="grid grid-cols-2 gap-3 text-sm">
            ${ltStat("p50 TTFT", fmtMs(m.ttft_p50_ms))}
            ${ltStat("p95 TTFT", fmtMs(m.ttft_p95_ms))}
            ${ltStat("Throughput", `${(m.throughput_tok_s || 0).toLocaleString()} tok/s`)}
            ${ltStat("Requests", `${m.requests_ok}/${m.requests_total}${m.requests_failed ? ` (${m.requests_failed} failed)` : ""}`)}
        </div>

        <div class="mt-4 pt-4 border-t border-slate-700/50">
            <div class="text-xs text-slate-500 mb-2">Work per pod (prefix-cache queries)</div>
            <div class="space-y-1.5">${spreadRows || '<span class="text-xs text-slate-600">no pod data</span>'}</div>
        </div>
    </div>`;
}

function ltStat(label, value) {
    return `<div class="bg-slate-900/60 rounded-lg p-3 flex flex-col">
        <span class="text-[11px] text-slate-500">${label}</span>
        <span class="font-mono text-slate-200">${value}</span></div>`;
}

function fmtMs(ms) {
    if (ms == null) return "n/a";
    return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

function renderLtHeadline(d) {
    const el = document.getElementById("lt-headline");
    const c = d.comparison;
    if (!c || !d.llmd || !d.direct) { el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    const speedup = c.ttft_p95_speedup ? `${c.ttft_p95_speedup}×` : "—";
    const tput = c.throughput_ratio ? `${c.throughput_ratio}×` : "—";
    const hitGain = c.hit_rate_gain != null ? `${(c.hit_rate_gain * 100).toFixed(0)} pts` : "—";
    el.innerHTML = `
        <div class="text-sm text-slate-400 mb-2">With llm-d, on the same shared GPU and identical workload:</div>
        <div class="flex flex-wrap justify-center gap-8">
            <div><span class="text-3xl font-bold text-sky-300">${speedup}</span><div class="text-xs text-slate-500">lower p95 TTFT</div></div>
            <div><span class="text-3xl font-bold text-emerald-300">+${hitGain}</span><div class="text-xs text-slate-500">cache hit rate</div></div>
            <div><span class="text-3xl font-bold text-sky-300">${tput}</span><div class="text-xs text-slate-500">throughput</div></div>
        </div>`;
}

// Backend identity + provisioning state (CPU/GPU, and whether the model server is up).
async function pollStatus() {
    try {
        const d = await (await fetch("/api/status")).json();
        const hw = d.hardware || (d.backend === "gpu" ? "GPU" : "CPU");

        const badge = document.getElementById("backend-badge");
        badge.innerText = `Backend: ${hw}`;
        badge.className = "text-xs font-bold uppercase tracking-wider px-3 py-1 rounded-full border " +
            (d.backend === "gpu" ? "border-fuchsia-500/60 bg-fuchsia-500/10 text-fuchsia-300"
                                 : "border-sky-500/60 bg-sky-500/10 text-sky-300");

        const banner = document.getElementById("provision-banner");
        const msg = document.getElementById("provision-message");
        const btn = document.getElementById("submit-btn");
        backendReady = (d.state === "ready");

        if (backendReady) {
            banner.classList.add("hidden");
        } else {
            msg.innerText = d.message || "Provisioning…";
            banner.className = "border-b px-6 py-3 flex items-center space-x-3 " +
                (d.state === "degraded" ? "bg-red-500/15 border-red-500/40 text-red-300"
                                        : "bg-amber-500/15 border-amber-500/40 text-amber-300");
        }
        if (btn) {
            btn.disabled = !backendReady;
            btn.classList.toggle("opacity-50", !backendReady);
            btn.classList.toggle("cursor-not-allowed", !backendReady);
        }
    } catch (err) {
        console.error("Failed to poll status", err);
    }
}

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
        const blocksCached = node.blocks_cached || 0;   // llm-d KV index (ground truth)
        const evictions = node.evictions || 0;
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
                    <span class="text-slate-500">KV blocks held <span class="text-slate-600">(llm-d index)</span></span>
                    <span class="font-mono text-sky-300">${blocksCached.toLocaleString()}</span>
                </div>
                <div class="flex flex-col">
                    <span class="text-slate-500">Evictions</span>
                    <span class="font-mono ${evictions > 0 ? 'text-amber-400' : 'text-slate-400'}">${evictions.toLocaleString()}</span>
                </div>
                <div class="flex flex-col">
                    <span class="text-slate-500">Cached tokens</span>
                    <span class="font-mono text-slate-300">${cachedTokens.toLocaleString()}</span>
                </div>
                <div class="flex flex-col">
                    <span class="text-slate-500">Running</span>
                    <span class="font-mono ${running > 0 ? 'text-emerald-400' : 'text-slate-400'}">${running} req</span>
                </div>
                <div class="flex flex-col col-span-2">
                    <span class="text-slate-500">KV cache usage</span>
                    <span class="font-mono text-slate-400">${Math.round((node.kv_cache_usage || 0) * 100)}% of GPU KV cache</span>
                </div>
            </div>
        `;
        container.appendChild(card);

        // Real eviction signal from vLLM KV events: flash + log when blocks are evicted.
        if (node.name in prevEvictions && evictions > prevEvictions[node.name]) {
            const n = evictions - prevEvictions[node.name];
            const podShort = node.name.replace(/^vllm-server-[^-]+-/, "");
            logEvent(`⚠ LRU eviction on pod ${podShort}: ${n} KV block(s) evicted (cache saturated).`, true);
            card.classList.add("border-amber-400", "bg-amber-950/30");
            setTimeout(() => card.classList.remove("border-amber-400", "bg-amber-950/30"), 2200);
        }
        prevEvictions[node.name] = evictions;
    });
}

// Execute Prompt Request (concurrent-friendly)
async function executeRequest() {
    const question = document.getElementById("prompt-input").value.trim();
    const context = document.getElementById("context-input").value.trim();
    if (!question) { alert("Please enter a question."); return; }
    if (!backendReady) { logEvent("Backend is still provisioning — please wait until it is Ready.", true); return; }

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
    streamBadge.innerText = `${ctxKey} → prefilling…`;
    // Set the latency expectation up front: a cold prefix is prefilled on the
    // worker before the first token; a warm cache hit skips most of that. On GPU
    // the first request to a pod/shape also pays a one-time ~20s JIT compile.
    logEvent(`Sent [${ctxKey}]. A cold prefix is prefilled before the first token; a warm cache hit returns faster. (First-ever request per pod also pays a one-time GPU JIT compile.)`);

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
            const podShort = servedBy ? servedBy.replace(/^vllm-server-[^-]+-/, "") : "?";
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
