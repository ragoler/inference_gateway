import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx

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

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            return response.json()
        except httpx.RequestError as exc:
            raise HTTPException(status_code=500, detail=f"An error occurred while requesting {exc.request.url!r}.")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=f"Error response {exc.response.status_code} while requesting {exc.request.url!r}.")

@app.get("/health")
async def health_check():
    return {"status": "ok", "gateway_ip_configured": bool(GATEWAY_IP)}
