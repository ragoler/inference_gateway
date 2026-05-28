import os
import pytest
import requests

APP_IP = os.getenv("APP_IP")
APP_PORT = os.getenv("APP_PORT", "80")

@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_generate_endpoint():
    url = f"http://{APP_IP}:{APP_PORT}/generate"
    payload = {
        "prompt": "Write a short poem about kubernetes",
        "max_tokens": 30,
        "temperature": 0.1
    }
    response = requests.post(url, json=payload, timeout=60)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    assert "text" in data["choices"][0]
    assert len(data["choices"][0]["text"]) > 0

@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_health_endpoint():
    url = f"http://{APP_IP}:{APP_PORT}/health"
    response = requests.get(url, timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["gateway_ip_configured"] is True
