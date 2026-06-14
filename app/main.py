import os
import json
import time
import asyncio
import threading
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

from loadtest_util import (
    make_documents, build_prompts, order_prompts, summarize, compare, PATTERNS,
)

# Attempt to import kubernetes (only available when running in-cluster)
try:
    from kubernetes import client, config
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    K8S_AVAILABLE = True
except Exception:
    K8S_AVAILABLE = False

# ZMQ + msgspec let the app consume vLLM's KV-cache events directly (the same
# stream the llm-d EPP indexes), to show a real per-pod block index + evictions.
try:
    import zmq
    import msgspec
    KV_EVENTS_AVAILABLE = True
except Exception:
    KV_EVENTS_AVAILABLE = False

app = FastAPI(title="Inference Gateway Client API")

GATEWAY_IP = os.getenv("GATEWAY_IP")
GATEWAY_NAME = os.getenv("GATEWAY_NAME", "inference-gw-gpu")
MODEL_NAME = os.getenv("MODEL_NAME", "Qwen/Qwen2.5-1.5B-Instruct")
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
POD_SELECTOR = os.getenv("POD_SELECTOR", "app=vllm-server")
METRICS_PORT = os.getenv("METRICS_PORT", "8000")
# Which hardware this cluster serves (shown in the UI). GPU-only since the demo
# pivoted to the load-test comparison; kept configurable for the badge.
BACKEND = os.getenv("BACKEND", "gpu")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "vllm-server")
# The "without llm-d" baseline: a plain ClusterIP Service over the same pods
# (cache-blind round-robin). The load-test runner targets this for the direct arm.
DIRECT_URL = os.getenv("DIRECT_URL", "http://vllm-direct")
# Tracks whether the model server was ever Ready, to distinguish first-time
# provisioning from re-provisioning after a spot reclaim.
_was_ready = False

# Number of /generate calls currently in their measured window. Used to flag
# when per-pod metric-delta attribution is unreliable (overlapping requests).
_active_generates = 0

_resolved_gateway_ip = None


def get_gateway_ip():
    """Resolve the inference Gateway address (env-first, in-cluster fallback).

    Standalone: setup_infra.sh injects the real GATEWAY_IP env var, which is used
    verbatim (behavior unchanged). When deployed through the GKE Showcase Hub the env
    var is absent (or left as an unexpanded ``${GATEWAY_IP}`` placeholder), so we resolve
    the Gateway's programmed address from the K8s API and cache it. Returns None if it
    cannot be determined yet (caller surfaces a clear 500).
    """
    global _resolved_gateway_ip
    if GATEWAY_IP and not GATEWAY_IP.startswith("${"):
        return GATEWAY_IP
    if _resolved_gateway_ip:
        return _resolved_gateway_ip
    if not K8S_AVAILABLE:
        return None
    try:
        gw = client.CustomObjectsApi().get_namespaced_custom_object(
            group="gateway.networking.k8s.io",
            version="v1",
            namespace=NAMESPACE,
            plural="gateways",
            name=GATEWAY_NAME,
        )
        addresses = gw.get("status", {}).get("addresses", [])
        if addresses:
            _resolved_gateway_ip = addresses[0].get("value")
            return _resolved_gateway_ip
    except Exception:
        return None
    return None


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 50
    temperature: float = 0.0


# ---------------------------------------------------------------------------
# vLLM metric scraping helpers
#
# Real metric names exposed by vllm/vllm-openai (verified on the live
# cluster, vLLM 0.22.0):
#   vllm:kv_cache_usage_perc        -> KV cache utilization (0..1)
#   vllm:num_requests_waiting       -> queue depth
#   vllm:num_requests_running       -> in-flight requests
#   vllm:prefix_cache_queries_total -> cumulative prefix-cache block lookups
#   vllm:prefix_cache_hits_total    -> cumulative prefix-cache block hits
# ---------------------------------------------------------------------------

def _metric_value(line: str) -> float:
    """Parse the numeric value from a Prometheus exposition line."""
    try:
        return float(line.split()[-1])
    except (ValueError, IndexError):
        return 0.0


def list_vllm_pods():
    """Return [(pod_name, pod_ip), ...] for Running vLLM pods."""
    if not K8S_AVAILABLE:
        return []
    pods = v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=POD_SELECTOR)
    result = []
    for pod in pods.items:
        if pod.status.phase == "Running" and pod.status.pod_ip:
            result.append((pod.metadata.name, pod.status.pod_ip))
    return result


# ---------------------------------------------------------------------------
# Live KV-cache index (consumes vLLM KV-cache events over ZMQ)
#
# vLLM publishes BlockStored / BlockRemoved / AllBlocksCleared events per pod on
# tcp://<pod-ip>:5557, msgpack-encoded as [timestamp, [[tag, [block_hashes], ...]]].
# We subscribe to each pod and track the set of resident block hashes, so the UI
# can show a real per-pod block count and real evictions -- the same ground truth
# the llm-d EPP routes on. (Best-effort: if events are missed the counts drift
# until the next AllBlocksCleared; fine for a demo.)
# ---------------------------------------------------------------------------
KV_EVENTS_PORT = int(os.getenv("KV_EVENTS_PORT", "5557"))
_kv_lock = threading.Lock()
_kv_index = {}          # pod_ip -> {"name", "blocks": set, "evictions": int, "events": int}
_kv_subscribed = set()  # pod_ips with a running subscriber thread


def _kv_subscriber(pod_name: str, pod_ip: str):
    """Subscribe to one vLLM pod's KV-event socket and maintain its block set."""
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.setsockopt(zmq.RCVTIMEO, 5000)
    sock.connect(f"tcp://{pod_ip}:{KV_EVENTS_PORT}")
    with _kv_lock:
        _kv_index.setdefault(pod_ip, {"name": pod_name, "blocks": set(), "evictions": 0, "events": 0})
    misses = 0
    while True:
        try:
            frames = sock.recv_multipart()
        except zmq.Again:
            misses += 1
            if misses > 6:        # pod likely gone; let discovery re-add if it returns
                break
            continue
        except Exception:
            break
        misses = 0
        try:
            batch = msgspec.msgpack.decode(frames[-1])
            events = batch[1] if isinstance(batch, list) and len(batch) > 1 else []
        except Exception:
            continue
        with _kv_lock:
            st = _kv_index.setdefault(pod_ip, {"name": pod_name, "blocks": set(), "evictions": 0, "events": 0})
            for ev in events:
                if not ev:
                    continue
                tag = ev[0]
                st["events"] += 1
                if tag == "BlockStored" and len(ev) > 1 and ev[1]:
                    st["blocks"].update(ev[1])
                elif tag == "BlockRemoved" and len(ev) > 1 and ev[1]:
                    for h in ev[1]:
                        st["blocks"].discard(h)
                    st["evictions"] += len(ev[1])
                elif tag == "AllBlocksCleared":
                    st["evictions"] += len(st["blocks"])
                    st["blocks"].clear()
    try:
        sock.close(0)
    except Exception:
        pass
    with _kv_lock:
        _kv_subscribed.discard(pod_ip)


def _kv_discovery_loop():
    """Ensure one subscriber thread per running vLLM pod."""
    while True:
        try:
            for name, ip in list_vllm_pods():
                start = False
                with _kv_lock:
                    if ip not in _kv_subscribed:
                        _kv_subscribed.add(ip)
                        start = True
                if start:
                    threading.Thread(target=_kv_subscriber, args=(name, ip), daemon=True).start()
        except Exception:
            pass
        time.sleep(10)


def kv_index_snapshot():
    """pod_name -> {blocks_cached, evictions} from the live KV index."""
    out = {}
    with _kv_lock:
        for st in _kv_index.values():
            out[st["name"]] = {"blocks_cached": len(st["blocks"]), "evictions": st["evictions"]}
    return out


@app.on_event("startup")
async def _start_kv_consumer():
    if K8S_AVAILABLE and KV_EVENTS_AVAILABLE:
        threading.Thread(target=_kv_discovery_loop, daemon=True).start()


async def scrape_pod(http_client: httpx.AsyncClient, pod_ip: str) -> dict:
    """Scrape one vLLM pod's /metrics endpoint."""
    metrics = {
        "kv_cache_usage": 0.0,
        "queue_length": 0,
        "running": 0,
        "prefix_queries": 0.0,
        "prefix_hits": 0.0,
        "cached_tokens": 0.0,
    }
    try:
        resp = await http_client.get(f"http://{pod_ip}:{METRICS_PORT}/metrics", timeout=2.0)
        if resp.status_code != 200:
            return metrics
        for line in resp.text.split("\n"):
            if not line or line.startswith("#"):
                continue
            if line.startswith("vllm:kv_cache_usage_perc"):
                metrics["kv_cache_usage"] = _metric_value(line)
            elif line.startswith("vllm:num_requests_waiting"):
                metrics["queue_length"] = int(_metric_value(line))
            elif line.startswith("vllm:num_requests_running"):
                metrics["running"] = int(_metric_value(line))
            elif line.startswith("vllm:prefix_cache_queries_total"):
                metrics["prefix_queries"] = _metric_value(line)
            elif line.startswith("vllm:prefix_cache_hits_total"):
                metrics["prefix_hits"] = _metric_value(line)
            elif line.startswith("vllm:prompt_tokens_cached_total"):
                metrics["cached_tokens"] = _metric_value(line)
    except Exception:
        pass
    return metrics


async def snapshot_pods(http_client: httpx.AsyncClient) -> dict:
    """Return {pod_name: metrics} for all running vLLM pods."""
    snapshot = {}
    for name, ip in list_vllm_pods():
        snapshot[name] = await scrape_pod(http_client, ip)
    return snapshot


def compute_routing(before: dict, after: dict) -> dict:
    """Attribute a request to a pod by comparing prefix-cache counter deltas.

    The pod whose prefix_cache_queries_total grew the most served the request.
    Whether prefix_cache_hits_total also grew tells us hit vs. cold prefill.
    Reliable for presenter-paced (one-at-a-time) demos; under heavy concurrent
    traffic deltas can blend across requests.
    """
    # Rank pods by how many prefix-cache blocks they queried during the window.
    deltas = []
    for name, post in after.items():
        pre = before.get(name, {})
        dq = post.get("prefix_queries", 0.0) - pre.get("prefix_queries", 0.0)
        dh = post.get("prefix_hits", 0.0) - pre.get("prefix_hits", 0.0)
        deltas.append((dq, dh, name))
    deltas.sort(reverse=True)

    served_by = None
    queries_delta = hits_delta = 0.0
    dominant = False
    if deltas and deltas[0][0] > 0:
        queries_delta, hits_delta, served_by = deltas[0]
        second = deltas[1][0] if len(deltas) > 1 else 0.0
        # "Dominant" = this pod clearly accounts for the request: a real number of
        # block queries AND well ahead of any other pod's. Otherwise attribution
        # is ambiguous (overlap/noise) and we don't trust the served_by/hit.
        dominant = queries_delta >= 16 and queries_delta >= 3 * second

    hit_ratio = (hits_delta / queries_delta) if queries_delta > 0 else 0.0
    # Only call it a cache hit if a meaningful fraction of blocks were reused
    # (filters stray single-block hits that the old `>0` test misreported).
    cache_hit = dominant and hit_ratio >= 0.15
    return {
        "available": served_by is not None and dominant,
        "served_by": served_by if dominant else None,
        "cache_hit": cache_hit,
        "queries_delta": queries_delta,
        "hits_delta": hits_delta,
        "hit_ratio": round(hit_ratio, 3),
    }


@app.post("/generate")
async def generate_text(request: GenerateRequest):
    gw = get_gateway_ip()
    if not gw:
        raise HTTPException(status_code=500, detail="Gateway address unavailable: set GATEWAY_IP or ensure the in-cluster Gateway is programmed")

    url = f"http://{gw}:80/v1/completions"
    payload = {
        "model": MODEL_NAME,
        "prompt": request.prompt,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    # When all pods are busy (or paying a one-time GPU JIT compile) the gateway
    # can briefly return 503 (backend saturation). Retry a few times so quick demo
    # clicks don't fail spuriously.
    global _active_generates
    MAX_ATTEMPTS = 4
    RETRY_STATUSES = {503, 429}

    _active_generates += 1
    try:
      async with httpx.AsyncClient() as http_client:
        before = {}
        full_text = ""
        usage = None
        ttft_ms = None
        start = None
        last_status = None
        overlap = False

        for attempt in range(MAX_ATTEMPTS):
            # Snapshot per-pod prefix-cache counters right BEFORE the attempt so
            # we can attribute which pod served it (and if it was a cache hit).
            before = await snapshot_pods(http_client)
            overlap = overlap or _active_generates > 1
            full_text = ""
            usage = None
            ttft_ms = None
            start = time.perf_counter()
            try:
                async with http_client.stream("POST", url, json=payload, timeout=120.0) as resp:
                    if resp.status_code in RETRY_STATUSES:
                        last_status = resp.status_code
                        await resp.aread()
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if choices:
                            text = choices[0].get("text", "")
                            if text and ttft_ms is None:
                                # Time-to-first-token: first chunk carrying text.
                                ttft_ms = (time.perf_counter() - start) * 1000.0
                            full_text += text
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                break  # success
            except httpx.HTTPStatusError as exc:
                raise HTTPException(
                    status_code=exc.response.status_code,
                    detail=f"Gateway returned {exc.response.status_code}.",
                )
            except httpx.RequestError as exc:
                raise HTTPException(status_code=500, detail=f"Error requesting gateway: {exc}")
        else:
            # All attempts exhausted on a retryable status.
            raise HTTPException(
                status_code=503,
                detail=f"Gateway saturated (status {last_status}) after {MAX_ATTEMPTS} attempts. "
                       f"Backends are busy; try again.",
            )

        total_ms = (time.perf_counter() - start) * 1000.0

        # Snapshot AFTER and diff to find the serving pod + hit/miss.
        after = await snapshot_pods(http_client)
        overlap = overlap or _active_generates > 1
        routing = compute_routing(before, after)
        # Metric-delta attribution is only reliable when one request runs at a
        # time. If others overlapped this window, mark it approximate.
        routing["confidence"] = "approximate" if overlap else "exact"

        # Keep `choices`/`usage` at the top level for OpenAI-style compatibility.
        return {
            "model": MODEL_NAME,
            "choices": [{"index": 0, "text": full_text, "finish_reason": "stop"}],
            "usage": usage,
            "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
            "total_ms": round(total_ms, 1),
            "routing": routing,
        }
    finally:
        _active_generates -= 1


@app.get("/health")
async def health_check():
    return {"status": "ok", "backend": BACKEND,
            "gateway_ip_configured": bool(get_gateway_ip()), "k8s_available": K8S_AVAILABLE}


def _model_status():
    """(ready_replicas, desired, degraded_reason) for the model-server Deployment."""
    if not K8S_AVAILABLE:
        return 2, 2, None  # local/dev: pretend Ready
    ready = desired = 0
    try:
        # Read the Deployment object (NOT the /status subresource, which needs a
        # separate deployments/status RBAC grant); .status is included here.
        dep = apps_v1.read_namespaced_deployment(MODEL_DEPLOYMENT_NAME, NAMESPACE)
        desired = dep.spec.replicas or 0
        ready = dep.status.ready_replicas or 0
    except Exception:
        pass
    # Detect a hard failure (image/crash) vs. normal provisioning.
    degraded = None
    try:
        pods = v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=POD_SELECTOR)
        for pod in pods.items:
            for cs in (pod.status.container_statuses or []):
                waiting = getattr(cs.state, "waiting", None)
                if waiting and waiting.reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                    degraded = waiting.reason
    except Exception:
        pass
    return ready, desired, degraded


@app.get("/api/status")
async def get_status():
    """Backend identity + provisioning state, so the UI can show provisioning/
    re-provisioning and keep polling until the model server is Ready."""
    global _was_ready
    ready, desired, degraded = _model_status()
    hw = "GPU" if BACKEND == "gpu" else "CPU"

    if degraded and ready == 0:
        state, message = "degraded", f"Model server error: {degraded}."
    elif ready >= 1:
        _was_ready = True
        state, message = "ready", f"Ready on {hw}."
    elif _was_ready:
        state = "reprovisioning"
        message = f"{hw} compute was reclaimed (spot) — re-provisioning…"
    else:
        state = "provisioning"
        message = f"Provisioning {hw} compute… (waiting for a node + model load)"

    return {"backend": BACKEND, "hardware": hw, "state": state,
            "ready_replicas": ready, "desired": desired, "message": message}


@app.get("/api/telemetry")
async def get_telemetry():
    if not K8S_AVAILABLE:
        # Fallback for local verification/testing without a cluster.
        return {
            "nodes": [
                {"name": "vllm-server-local-1", "kv_cache_usage": 0.0, "queue_length": 0,
                 "running": 1, "hit_ratio": 0.0, "cached_tokens": 0, "blocks_cached": 0, "evictions": 0},
                {"name": "vllm-server-local-2", "kv_cache_usage": 0.0, "queue_length": 1,
                 "running": 0, "hit_ratio": 0.0, "cached_tokens": 0, "blocks_cached": 0, "evictions": 0},
            ],
            "kv_events": False,
        }

    try:
        nodes = []
        kv = kv_index_snapshot()  # llm-d-style block index from live KV events
        async with httpx.AsyncClient() as http_client:
            for name, ip in list_vllm_pods():
                m = await scrape_pod(http_client, ip)
                hit_ratio = (m["prefix_hits"] / m["prefix_queries"]) if m["prefix_queries"] > 0 else 0.0
                k = kv.get(name, {})
                nodes.append({
                    "name": name,
                    "kv_cache_usage": m["kv_cache_usage"],
                    "queue_length": m["queue_length"],
                    "running": m["running"],
                    "hit_ratio": round(hit_ratio, 3),
                    "cached_tokens": int(m["cached_tokens"]),
                    # Raw cumulative counters so the UI can compute a
                    # session-relative hit rate (resets on "New Run").
                    "prefix_queries": int(m["prefix_queries"]),
                    "prefix_hits": int(m["prefix_hits"]),
                    # Ground-truth KV block index from vLLM events (llm-d signal).
                    "blocks_cached": k.get("blocks_cached", 0),
                    "evictions": k.get("evictions", 0),
                })
        return {"nodes": nodes, "kv_events": KV_EVENTS_AVAILABLE}
    except Exception as e:
        return {"error": str(e), "nodes": []}


# ---------------------------------------------------------------------------
# Load-test comparison: WITH llm-d (gateway) vs WITHOUT llm-d (plain Service)
#
# Fires an identical concurrent workload at each path and reports hit rate,
# p50/p95 TTFT, throughput, and how work spread across pods. The headline:
# cache-aware routing (llm-d) reuses prefixes -> higher hit rate -> the shared
# GPU wastes less compute on redundant prefill -> better tail latency/throughput.
#
# Hit rate is measured cluster-wide (sum of vLLM prefix_cache_{hits,queries}_total
# deltas across pods) so it stays correct under concurrency, where per-request
# delta attribution is racy. Each mode uses its own fresh document nonce so the
# two arms never share a warm cache.
# ---------------------------------------------------------------------------

class LoadTestRequest(BaseModel):
    concurrency: int = 8
    num_docs: int = 8
    queries_per_doc: int = 6
    max_tokens: int = 64
    # Request dispatch order: grouped | shuffle | stagger | interleave (see
    # loadtest_util.order_prompts). Lets the presenter pick the traffic shape.
    pattern: str = "grouped"


_loadtest = {
    "running": False,
    "phase": "idle",     # idle | running:direct | running:llmd | done | error
    "params": None,
    "direct": None,
    "llmd": None,
    "comparison": None,
    "error": None,
}
_loadtest_seq = 0


async def _sum_prefix_counters(http_client) -> tuple:
    """(total_queries, total_hits, {pod: queries}) across all running vLLM pods."""
    q = h = 0.0
    per_pod = {}
    for name, ip in list_vllm_pods():
        m = await scrape_pod(http_client, ip)
        q += m["prefix_queries"]
        h += m["prefix_hits"]
        per_pod[name] = m["prefix_queries"]
    return q, h, per_pod


async def _one_request(http_client, base_url, prompt, max_tokens, sem) -> dict:
    """Issue one streamed completion; measure TTFT and output tokens."""
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    async with sem:
        start = time.perf_counter()
        ttft_ms = None
        out_tokens = 0
        ok = False
        try:
            async with http_client.stream(
                "POST", f"{base_url}/v1/completions", json=payload, timeout=90.0
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    return {"ok": False, "ttft_ms": None, "out_tokens": 0}
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices and choices[0].get("text") and ttft_ms is None:
                        ttft_ms = (time.perf_counter() - start) * 1000.0
                    if chunk.get("usage"):
                        out_tokens = chunk["usage"].get("completion_tokens", out_tokens)
                ok = True
        except Exception:
            ok = False
        return {"ok": ok, "ttft_ms": ttft_ms, "out_tokens": out_tokens}


async def _run_mode(http_client, base_url, docs, p, seed) -> dict:
    """Run the full workload against one base_url and summarize it.

    Requests are dispatched in ordered *waves* (see order_prompts): most patterns
    are a single wave; `stagger` is two (prime, then repeats). `seed` is shared by
    both comparison arms so a shuffled order is identical for each — apples-to-apples.
    """
    prompts = build_prompts(docs, p.queries_per_doc)
    waves = order_prompts(prompts, p.pattern, seed=seed)
    sem = asyncio.Semaphore(max(1, p.concurrency))

    q0, h0, pod0 = await _sum_prefix_counters(http_client)
    t0 = time.perf_counter()
    records = []
    for wave in waves:
        wave_records = await asyncio.gather(
            *[_one_request(http_client, base_url, prompt, p.max_tokens, sem)
              for _doc_id, prompt in wave]
        )
        records.extend(wave_records)
    wall_s = time.perf_counter() - t0
    await asyncio.sleep(1.0)  # let cumulative counters settle after the burst
    q1, h1, pod1 = await _sum_prefix_counters(http_client)

    dq = max(q1 - q0, 0.0)
    dh = max(h1 - h0, 0.0)
    hit_rate = (dh / dq) if dq > 0 else 0.0
    pod_spread = {name: int(pod1.get(name, 0) - pod0.get(name, 0)) for name in pod1}
    return summarize(records, wall_s, hit_rate, pod_spread)


async def _run_loadtest(p: LoadTestRequest, nonce: str, seed: int):
    """Run both arms back-to-back with independent document sets."""
    global _loadtest
    gateway_url = f"http://{get_gateway_ip()}:80"
    try:
        async with httpx.AsyncClient() as http_client:
            # Direct (no llm-d) first, then llm-d — independent nonces so neither
            # benefits from the other's warm cache. Same `seed` so a shuffled
            # order is identical across arms (apples-to-apples).
            _loadtest["phase"] = "running:direct"
            direct_docs = make_documents(p.num_docs, f"d{nonce}")
            _loadtest["direct"] = await _run_mode(http_client, DIRECT_URL, direct_docs, p, seed)

            _loadtest["phase"] = "running:llmd"
            llmd_docs = make_documents(p.num_docs, f"l{nonce}")
            _loadtest["llmd"] = await _run_mode(http_client, gateway_url, llmd_docs, p, seed)

            _loadtest["comparison"] = compare(_loadtest["llmd"], _loadtest["direct"])
            _loadtest["phase"] = "done"
    except Exception as e:
        _loadtest["error"] = str(e)
        _loadtest["phase"] = "error"
    finally:
        _loadtest["running"] = False


@app.post("/api/loadtest")
async def start_loadtest(req: LoadTestRequest):
    """Kick off a comparison run in the background; poll /api/loadtest/status."""
    global _loadtest, _loadtest_seq
    if not get_gateway_ip():
        raise HTTPException(status_code=500, detail="Gateway address unavailable: set GATEWAY_IP or ensure the in-cluster Gateway is programmed")
    if _loadtest["running"]:
        raise HTTPException(status_code=409, detail="A load test is already running.")
    # Clamp inputs so a stray UI value can't launch thousands of requests.
    req.concurrency = max(1, min(req.concurrency, 64))
    req.num_docs = max(1, min(req.num_docs, 64))
    req.queries_per_doc = max(1, min(req.queries_per_doc, 32))
    req.max_tokens = max(1, min(req.max_tokens, 256))
    if req.pattern not in PATTERNS:
        req.pattern = "grouped"

    _loadtest_seq += 1
    seed = _loadtest_seq
    nonce = f"{_loadtest_seq}{int(time.time()) % 100000}"
    _loadtest = {
        "running": True, "phase": "running:direct", "params": req.dict(),
        "direct": None, "llmd": None, "comparison": None, "error": None,
    }
    asyncio.create_task(_run_loadtest(req, nonce, seed))
    total = req.num_docs * req.queries_per_doc
    return {"started": True, "requests_per_mode": total, "params": req.dict()}


@app.get("/api/loadtest/status")
async def loadtest_status():
    """Current load-test progress + per-mode results + headline comparison."""
    return _loadtest


# Explicitly serve index.html at root route
@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")

# Mount static UI assets for scripts/styles
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
