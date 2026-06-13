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
    response = requests.post(f"{BASE_URL}/generate", json=payload, timeout=90)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    assert "text" in data["choices"][0]
    # New real-telemetry fields surfaced by the streaming proxy.
    assert "ttft_ms" in data
    assert "total_ms" in data
    assert "routing" in data
    assert "served_by" in data["routing"]

@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_health_endpoint():
    """Test application and gateway routing health state."""
    response = requests.get(f"{BASE_URL}/health", timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["gateway_ip_configured"] is True


@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_status_endpoint():
    """Backend identity + provisioning state machine."""
    response = requests.get(f"{BASE_URL}/api/status", timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert data["state"] in ("ready", "provisioning", "reprovisioning", "degraded")
    assert "hardware" in data


@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_loadtest_status_endpoint():
    """The load-test status endpoint is always queryable (even before any run)."""
    response = requests.get(f"{BASE_URL}/api/loadtest/status", timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert "phase" in data
    assert "running" in data


@pytest.mark.skipif(not APP_IP, reason="APP_IP environment variable not set")
def test_loadtest_run_comparison():
    """Kick off a tiny comparison run and verify both arms report metrics."""
    import time

    payload = {"concurrency": 2, "num_docs": 2, "queries_per_doc": 2, "max_tokens": 8}
    start = requests.post(f"{BASE_URL}/api/loadtest", json=payload, timeout=15)
    # 409 means one is already running — acceptable for a shared environment.
    assert start.status_code in (200, 409)

    # Poll for completion (small workload; allow generous time for GPU JIT warmup).
    deadline = time.time() + 240
    data = {}
    while time.time() < deadline:
        data = requests.get(f"{BASE_URL}/api/loadtest/status", timeout=10).json()
        if not data.get("running") and data.get("phase") in ("done", "error"):
            break
        time.sleep(3)

    assert data.get("phase") == "done", f"load test did not finish: {data}"
    for mode in ("llmd", "direct"):
        m = data[mode]
        assert m["requests_total"] == 4  # 2 docs * 2 queries
        assert "ttft_p95_ms" in m
        assert "hit_rate" in m
        assert "throughput_tok_s" in m
    assert "comparison" in data
