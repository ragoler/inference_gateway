import os
import pytest
import requests

APP_IP = os.getenv("APP_IP")
APP_PORT = os.getenv("APP_PORT", "80")
BASE_URL = f"http://{APP_IP}:{APP_PORT}" if APP_IP else ""

@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_ui_index_endpoint():
    """Test that the Static UI template successfully loads."""
    response = requests.get(BASE_URL, timeout=10)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("Content-Type", "")
    assert "GKE Inference Gateway" in response.text
    assert "nodes-grid" in response.text

@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_telemetry_endpoint():
    """Test that the live telemetry proxy returns pod node metrics."""
    response = requests.get(f"{BASE_URL}/api/telemetry", timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert "nodes" in data
    assert isinstance(data["nodes"], list)

@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_generate_endpoint():
    """Test the active Inference Gateway generation stream."""
    payload = {
        "prompt": "Write a short poem about kubernetes",
        "max_tokens": 30,
        "temperature": 0.1
    }
    response = requests.post(f"{BASE_URL}/generate", json=payload, timeout=60)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    assert "text" in data["choices"][0]

@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_health_endpoint():
    """Test application and gateway routing health state."""
    response = requests.get(f"{BASE_URL}/health", timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["gateway_ip_configured"] is True
