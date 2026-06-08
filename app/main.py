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
MODEL_NAME = os.getenv("MODEL_NAME", "Qwen/Qwen2.5-1.5B-Instruct")
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
POD_SELECTOR = os.getenv("POD_SELECTOR", "app=vllm-server")
METRICS_PORT = os.getenv("METRICS_PORT", "8000")
# Which hardware this cluster serves (shown in the UI). One backend per cluster.
BACKEND = os.getenv("BACKEND", "cpu")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "vllm-server")
# Tracks whether the model server was ever Ready, to distinguish first-time
# provisioning from re-provisioning after a spot reclaim.
_was_ready = False

# Number of /generate calls currently in their measured window. Used to flag
# when per-pod metric-delta attribution is unreliable (overlapping requests).
_active_generates = 0


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 50
    temperature: float = 0.0


# ---------------------------------------------------------------------------
# vLLM metric scraping helpers
#
# Real metric names exposed by vllm/vllm-openai-cpu (verified on the live
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
    if not GATEWAY_IP:
        raise HTTPException(status_code=500, detail="GATEWAY_IP environment variable not set")

    url = f"http://{GATEWAY_IP}:80/v1/completions"
    payload = {
        "model": MODEL_NAME,
        "prompt": request.prompt,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    # CPU prefill is slow (~10s), so when both pods are busy the gateway briefly
    # returns 503 (backend saturation). Retry a few times so quick demo clicks
    # don't fail spuriously.
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
            "gateway_ip_configured": bool(GATEWAY_IP), "k8s_available": K8S_AVAILABLE}


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
                {"name": "vllm-cpu-server-local-1", "kv_cache_usage": 0.0, "queue_length": 0,
                 "running": 1, "hit_ratio": 0.0, "cached_tokens": 0, "blocks_cached": 0, "evictions": 0},
                {"name": "vllm-cpu-server-local-2", "kv_cache_usage": 0.0, "queue_length": 1,
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
    except Exception as e:
        return {"error": str(e), "nodes": []}


# Explicitly serve index.html at root route
@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")

# Mount static UI assets for scripts/styles
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
