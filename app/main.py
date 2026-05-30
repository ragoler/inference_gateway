import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

# Attempt to import kubernetes
try:
    from kubernetes import client, config
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    K8S_AVAILABLE = True
except Exception:
    K8S_AVAILABLE = False

app = FastAPI(title="Inference Gateway Client API")

GATEWAY_IP = os.getenv("GATEWAY_IP")
MODEL_NAME = os.getenv("MODEL_NAME", "Qwen/Qwen2.5-1.5B-Instruct")

class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 50
    temperature: float = 0.0

@app.post("/generate")
async def generate_text(request: GenerateRequest):
    if not GATEWAY_IP:
        raise HTTPException(status_code=500, detail="GATEWAY_IP environment variable not set")

    url = f"http://{GATEWAY_IP}:80/v1/completions"
    payload = {
        "model": MODEL_NAME,
        "prompt": request.prompt,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature
    }

    async with httpx.AsyncClient() as http_client:
        try:
            response = await http_client.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            return response.json()
        except httpx.RequestError as exc:
            raise HTTPException(status_code=500, detail=f"An error occurred while requesting {exc.request.url!r}.")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=f"Error response {exc.response.status_code} while requesting {exc.request.url!r}.")

@app.get("/health")
async def health_check():
    return {"status": "ok", "gateway_ip_configured": bool(GATEWAY_IP), "k8s_available": K8S_AVAILABLE}

@app.get("/api/telemetry")
async def get_telemetry():
    if not K8S_AVAILABLE:
        # Fallback for local verification/testing
        return {
            "nodes": [
                {"name": "vllm-cpu-server-local-1", "kv_cache_usage": 0.45, "queue_length": 0},
                {"name": "vllm-cpu-server-local-2", "kv_cache_usage": 0.12, "queue_length": 1}
            ]
        }

    try:
        # Discover pods directly via Kubernetes API
        pods = v1.list_namespaced_pod(namespace="default", label_selector="app=vllm-cpu-server")
        nodes = []
        async with httpx.AsyncClient() as http_client:
            for pod in pods.items:
                if pod.status.phase != "Running":
                    continue
                pod_ip = pod.status.pod_ip
                pod_name = pod.metadata.name
                kv_cache = 0.0
                queue_len = 0
                if pod_ip:
                    try:
                        # Scrape Prometheus metrics port
                        resp = await http_client.get(f"http://{pod_ip}:8000/metrics", timeout=2.0)
                        if resp.status_code == 200:
                            for line in resp.text.split("\n"):
                                if line.startswith("vllm:gpu_cache_usage"):
                                    parts = line.split(" ")
                                    if len(parts) >= 2:
                                        kv_cache = float(parts[-1])
                                elif line.startswith("vllm:num_requests_waiting"):
                                    parts = line.split(" ")
                                    if len(parts) >= 2:
                                        queue_len = int(float(parts[-1]))
                    except Exception:
                        pass
                nodes.append({
                    "name": pod_name,
                    "kv_cache_usage": kv_cache,
                    "queue_length": queue_len
                })
        return {"nodes": nodes}
    except Exception as e:
        return {"error": str(e), "nodes": []}

# Explicitly serve index.html at root route
@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")

# Mount static UI assets for scripts/styles
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
