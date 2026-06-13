"""Pure helpers for the load-test comparison demo.

Deliberately dependency-free (stdlib only) so it can be unit-tested without
FastAPI / httpx / kubernetes installed. main.py imports these; the async HTTP
plumbing that actually drives the load lives there.
"""

# Distinct topical documents so each one is a different prefix (the llm-d EPP
# should route all queries about a given document to the same pod; round-robin
# scatters them). Kept long enough to span many KV blocks.
_TOPICS = [
    "Quarterly financial results", "Clinical trial safety report",
    "Thermal engineering specification", "Distributed systems design review",
    "Supply chain risk assessment", "Cybersecurity incident postmortem",
    "Renewable energy feasibility study", "Pharmacovigilance summary",
    "Aerospace materials analysis", "Macroeconomic outlook briefing",
    "Genomic sequencing methodology", "Autonomous navigation white paper",
]

_SENTENCES = [
    "The committee reviewed the prior period and confirmed the headline figures.",
    "Methodology followed the pre-registered protocol with independent oversight.",
    "Operating tolerances were validated across the full environmental range.",
    "Cross-functional stakeholders signed off after a structured risk review.",
    "Measured outcomes exceeded the baseline by a statistically meaningful margin.",
    "Sensitivity analysis isolated the dominant drivers from secondary effects.",
    "Mitigations were prioritized by expected impact and implementation cost.",
    "Long-term monitoring will track regression against the established controls.",
    "The appendix records assumptions, exclusions, and the audit trail in full.",
    "Recommendations were graded and mapped to owners with explicit deadlines.",
]


def make_documents(num_docs, nonce, sentences_per_doc=18):
    """Return [{"id", "text"}] of `num_docs` distinct multi-block documents.

    `nonce` makes every document a fresh prefix (so a previous run's cache never
    leaks into this one). Each doc repeats sentences to comfortably exceed the KV
    block size and give the prefix-cache scorer several full blocks to match.
    """
    docs = []
    for i in range(num_docs):
        topic = _TOPICS[i % len(_TOPICS)]
        body = " ".join(
            _SENTENCES[(i + j) % len(_SENTENCES)] for j in range(sentences_per_doc)
        )
        text = f"Reference dossier {nonce}-{i}: {topic}. {body}"
        docs.append({"id": i, "text": text})
    return docs


def build_prompts(docs, queries_per_doc):
    """Expand documents into the full request list (doc prefix + varied question).

    All `queries_per_doc` prompts for a document share the SAME long prefix, so
    after the first one the prefix is cached — if routing sends them to the pod
    that holds it. Returns [(doc_id, prompt), ...].
    """
    prompts = []
    for d in docs:
        for qi in range(queries_per_doc):
            prompt = (
                f"{d['text']}\n\n"
                f"Question {qi + 1}: Summarize key finding number {qi + 1}.\nAnswer:"
            )
            prompts.append((d["id"], prompt))
    return prompts


def percentile(values, p):
    """Linear-interpolated percentile (p in 0..100). 0.0 for empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def summarize(records, wall_s, hit_rate, pod_spread):
    """Aggregate per-request records into the headline metrics for one mode.

    records: [{"ok": bool, "ttft_ms": float|None, "out_tokens": int}, ...]
    wall_s:  wall-clock seconds for the whole mode.
    hit_rate: cluster-wide prefix-cache hit fraction over the run (0..1).
    pod_spread: {pod_name: prefix_query_delta} — how work spread across pods.
    """
    oks = [r for r in records if r.get("ok")]
    ttfts = [r["ttft_ms"] for r in oks if r.get("ttft_ms") is not None]
    out_tokens = sum(r.get("out_tokens", 0) for r in oks)
    return {
        "requests_total": len(records),
        "requests_ok": len(oks),
        "requests_failed": len(records) - len(oks),
        "ttft_p50_ms": round(percentile(ttfts, 50), 1),
        "ttft_p95_ms": round(percentile(ttfts, 95), 1),
        "ttft_mean_ms": round(sum(ttfts) / len(ttfts), 1) if ttfts else 0.0,
        "throughput_tok_s": round(out_tokens / wall_s, 1) if wall_s > 0 else 0.0,
        "hit_rate": round(hit_rate, 3),
        "wall_ms": round(wall_s * 1000.0, 1),
        "pod_spread": pod_spread,
    }


def compare(llmd, direct):
    """Headline deltas between the two modes (for the UI banner). Safe on None."""
    if not llmd or not direct:
        return {}
    out = {}
    # p95 TTFT speedup: how much faster the warm-cache path's tail latency is.
    if llmd.get("ttft_p95_ms", 0) > 0:
        out["ttft_p95_speedup"] = round(direct["ttft_p95_ms"] / llmd["ttft_p95_ms"], 2)
    if direct.get("throughput_tok_s", 0) > 0:
        out["throughput_ratio"] = round(
            llmd["throughput_tok_s"] / direct["throughput_tok_s"], 2
        )
    out["hit_rate_gain"] = round(llmd.get("hit_rate", 0) - direct.get("hit_rate", 0), 3)
    return out