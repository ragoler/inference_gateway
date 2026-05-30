// Context Presets
const PRESETS = {
    Alpha: {
        name: "Context Alpha (Financial Spec)",
        color: "border-sky-500/50 text-sky-400 bg-sky-500/10",
        streamClass: "bg-sky-500/20 border-sky-500 text-sky-300",
        prompt: "What is the primary financial target outlined in the specification?"
    },
    Beta: {
        name: "Context Beta (Medical Report)",
        color: "border-emerald-500/50 text-emerald-400 bg-emerald-500/10",
        streamClass: "bg-emerald-500/20 border-emerald-500 text-emerald-300",
        prompt: "List the side effects described in the active clinical study."
    }
};

let currentContext = null;
let telemetryInterval = null;

// Initialize UI
document.addEventListener("DOMContentLoaded", () => {
    setupDropZone();
    pollTelemetry();
    telemetryInterval = setInterval(pollTelemetry, 2500);
});

// Load preset data
function loadPreset(key) {
    const preset = PRESETS[key];
    currentContext = {
        key: key,
        name: preset.name,
        colorClass: preset.color,
        streamClass: preset.streamClass
    };

    document.getElementById("active-doc").innerHTML = `Active: ${preset.name}`;
    document.getElementById("active-doc").classList.remove("hidden");
    document.getElementById("prompt-input").value = preset.prompt;

    logEvent(`Ingested ${preset.name}. Ingress stream mapped.`);
}

// File Drag and Drop logic
function setupDropZone() {
    const dropZone = document.getElementById("drop-zone");
    const fileInput = document.getElementById("file-input");

    dropZone.addEventListener("click", () => fileInput.click());
    
    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("border-sky-400");
    });

    dropZone.addEventListener("dragleave", () => {
        dropZone.classList.remove("border-sky-400");
    });

    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("border-sky-400");
        if (e.dataTransfer.files.length) {
            handleFile(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener("change", () => {
        if (fileInput.files.length) handleFile(fileInput.files[0]);
    });
}

function handleFile(file) {
    currentContext = {
        key: "Custom",
        name: `Custom (${file.name})`,
        colorClass: "border-purple-500/50 text-purple-400 bg-purple-500/10",
        streamClass: "bg-purple-500/20 border-purple-500 text-purple-300"
    };

    document.getElementById("active-doc").innerHTML = `Active: ${file.name}`;
    document.getElementById("active-doc").classList.remove("hidden");
    document.getElementById("prompt-input").value = "Summarize the attached text document.";

    logEvent(`Ingested custom payload: ${file.name}`);
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

// Render backend pod state cards
function renderNodes(nodes) {
    const container = document.getElementById("nodes-grid");
    container.innerHTML = "";

    nodes.forEach((node, idx) => {
        // Scale usage to 4GB physical setting
        const cachePercent = Math.min(Math.round(node.kv_cache_usage * 100), 100);
        const cacheGB = (node.kv_cache_usage * 4.0).toFixed(2);
        
        const card = document.createElement("div");
        card.className = "bg-slate-800 rounded-xl p-5 border border-slate-700 flex flex-col space-y-4 relative overflow-hidden";
        card.id = `node-card-${node.name}`;

        card.innerHTML = `
            <div class="flex justify-between items-start">
                <div>
                    <span class="text-xs font-mono text-slate-500 uppercase">Worker Pod ${idx + 1}</span>
                    <h3 class="text-sm font-bold text-white truncate w-48 font-mono mt-0.5">${node.name}</h3>
                </div>
                <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${node.queue_length > 0 ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30' : 'bg-slate-900 text-slate-400'}">
                    Queue: ${node.queue_length}
                </span>
            </div>

            <!-- KV Cache RAM Capacity Meter -->
            <div class="space-y-1.5">
                <div class="flex justify-between text-xs">
                    <span class="text-slate-400">KV Cache Allocation</span>
                    <span class="font-mono font-medium ${cachePercent > 85 ? 'text-amber-400' : 'text-sky-400'}">${cacheGB} / 4.0 GB (${cachePercent}%)</span>
                </div>
                <div class="w-full bg-slate-900 h-2 rounded-full overflow-hidden p-0.5 border border-slate-800">
                    <div class="h-full rounded-full transition-all duration-500 ${cachePercent > 85 ? 'bg-amber-400 shadow-md shadow-amber-400/20' : 'bg-sky-400'}" style="width: ${cachePercent}%"></div>
                </div>
            </div>

            <!-- Hardware Compute Footprint -->
            <div class="pt-2 border-t border-slate-700/50 flex justify-between items-center text-xs">
                <span class="text-slate-500">Hardware Subsystem</span>
                <span class="font-mono text-slate-400">CPU Engine (PagedAttention)</span>
            </div>
        `;
        container.appendChild(card);
    });
}

// Execute Prompt Request
async function executeRequest() {
    const prompt = document.getElementById("prompt-input").value.trim();
    if (!prompt) return alert("Please enter a prompt.");

    const btn = document.getElementById("submit-btn");
    const streamBadge = document.getElementById("routing-stream");

    // Setup UI tracing states
    btn.disabled = true;
    btn.innerHTML = `<span class="animate-spin">↻</span><span>Evaluating...</span>`;

    if (currentContext) {
        streamBadge.className = `text-xs px-3 py-1 rounded-full font-mono transition-all ${currentContext.streamClass}`;
        streamBadge.innerText = `Routing: ${currentContext.key} Payload`;
    } else {
        streamBadge.className = "text-xs px-3 py-1 rounded-full font-mono transition-all bg-slate-700 text-slate-300";
        streamBadge.innerText = "Routing: Cold Context Payload";
    }

    const startTime = performance.now();

    try {
        const response = await fetch("/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: prompt, max_tokens: 40 })
        });

        const duration = ((performance.now() - startTime) / 1000).toFixed(2);

        if (response.ok) {
            // Determine cache state based on speed profile
            const isCacheHit = duration < 1.0;
            const stateText = isCacheHit ? "KV Cache Hit" : "Cold Prefill";
            
            logEvent(`Completed in ${duration}s [${stateText}]. Payload routed successfully.`);
            
            // Flash background on target pods to simulate routing completion
            flashPodRouting(isCacheHit);
        } else {
            logEvent(`Inference request failed (HTTP ${response.status})`, true);
        }
    } catch (err) {
        logEvent(`Gateway execution error: ${err.message}`, true);
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<span>Send Request</span><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>`;
        setTimeout(() => {
            streamBadge.className = "text-xs px-3 py-1 rounded-full bg-slate-900 border border-slate-700 text-slate-400 font-mono transition-all";
            streamBadge.innerText = "Idle";
        }, 2000);
        pollTelemetry();
    }
}

function simulateEviction() {
    logEvent("⚠️ WARNING: Physical Cache Saturation threshold crossed. Applying LRU Eviction to Context Alpha.", true);
    // Force temporary UI feedback
    const streamBadge = document.getElementById("routing-stream");
    streamBadge.className = "text-xs px-3 py-1 rounded-full font-mono transition-all bg-amber-500/20 border border-amber-500 text-amber-300";
    streamBadge.innerText = "LRU Eviction Running";
    setTimeout(() => {
        streamBadge.className = "text-xs px-3 py-1 rounded-full bg-slate-900 border border-slate-700 text-slate-400 font-mono transition-all";
        streamBadge.innerText = "Idle";
    }, 2000);
}

function flashPodRouting(isCacheHit) {
    const cards = document.querySelectorAll("#nodes-grid > div");
    if (!cards.length) return;
    
    // Pick first node on cache hit, toggle on miss to simulate intelligent load spreading
    const targetCard = isCacheHit ? cards[0] : cards[cards.length - 1];
    
    const flashClass = isCacheHit ? "border-sky-400 bg-sky-950/30" : "border-emerald-400 bg-emerald-950/30";
    
    targetCard.classList.add(...flashClass.split(" "));
    setTimeout(() => {
        targetCard.classList.remove(...flashClass.split(" "));
    }, 1200);
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
