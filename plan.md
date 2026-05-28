# Plan: GKE Cluster with Inference Gateway and FastAPI Application

## Objective
Automate the setup of a Google Kubernetes Engine (GKE) cluster configured with the Kubernetes Gateway API Inference Extension, routing traffic to a CPU-based vLLM model server as outlined in `guide.pdf`. Additionally, develop and deploy a Python FastAPI application to the cluster that interacts with this inference gateway.

## Proposed Architecture
1.  **Infrastructure**: GKE Cluster with Gateway API enabled.
2.  **Inference Tier**:
    *   External L7 Gateway (`gke-l7-regional-external-managed`).
    *   Endpoint Picker (EPP) extension.
    *   InferencePool managing vLLM CPU model server pods.
3.  **Application Tier**:
    *   FastAPI application running in the cluster, exposing a user-friendly API and calling the Inference Gateway internally (or externally via its IP) to generate text.

---

## Step-by-Step Implementation Plan

### 1. Configuration Management (`.env`)
Create a `.env` file (and `.env.example`) to centralize configuration, similar to the sandbox example.
*   `PROJECT_ID`: Target GCP Project ID.
*   `CLUSTER_NAME`: Name of the GKE cluster (e.g., `cpu-inference-cluster`).
*   `ZONE`: GCP zone (e.g., `us-east5-a`).
*   `MACHINE_TYPE`: Node machine type (recommended: `e2-standard-16`).
*   `NUM_NODES`: Initial node count (recommended: `3`).
*   `MODEL_NAME`: HuggingFace model ID for vLLM (default: `Qwen/Qwen2.5-1.5B-Instruct`).
*   `KV_CACHE_SPACE`: VLLM CPU KV cache allocation in GB (default: `4`).
*   `GATEWAY_NAME`: Name of the Gateway resource (default: `inference-gateway`).

### 2. Infrastructure Manifests (`infra/`)
Prepare reusable Kubernetes manifests, using environment variable substitution where applicable.
*   **`infra/gateway.yaml`**: Defines the L7 Gateway.
*   **`infra/cpu-deployment.yaml`**: Defines the vLLM model server deployment, tuned for CPU (memory limits, `/dev/shm`, and `VLLM_CPU_KVCACHE_SPACE`).

### 3. Infrastructure Automation Script (`setup_infra.sh`)
Develop a robust shell script to automate the provisioning steps:
1.  Load and validate `.env` variables.
2.  Provision GKE cluster with `--gateway-api=standard`.
3.  Fetch cluster credentials.
4.  Install Gateway API Inference Extension CRDs.
5.  Apply `infra/gateway.yaml`.
6.  Apply `infra/cpu-deployment.yaml`.
7.  Execute `helm install` to deploy the `InferencePool` and EPP extension, automatically linking them to the deployed vLLM servers.
8.  Monitor and output the allocated External IP of the Gateway.

### 4. FastAPI Application Development (`app/`)
Create a lightweight FastAPI service that acts as a consumer of the LLM Gateway.
*   **`app/main.py`**: Contains the API logic. Exposes a `/generate` endpoint that accepts a prompt, formats it, and forwards it to the Inference Gateway's `/v1/completions` endpoint.
*   **`app/requirements.txt`**: Python dependencies (`fastapi`, `uvicorn`, `httpx`).
*   **`app/Dockerfile`**: Containerization instructions for the FastAPI app.

### 5. Application Deployment (`k8s/` & `deploy_app.sh`)
Prepare manifests and a script to deploy the FastAPI app to the same GKE cluster.
*   **`k8s/app-deployment.yaml`**: Deployment for the FastAPI container.
*   **`k8s/app-service.yaml`**: LoadBalancer service to expose the FastAPI application to the end-user.
*   **`deploy_app.sh`**: Script to build the Docker image, push it to Google Artifact Registry (or a specified registry), and apply the `k8s/` manifests.

### 6. Automated Verification and Testing (`tests/` & `verify_setup.sh`)
*   **`tests/test_integration.py`**: Automated test script using `pytest` to verify the full pipeline. It will:
    *   Query the FastAPI application's `/generate` endpoint.
    *   Assert a successful response and valid content generation.
*   **`verify_setup.sh`**: Script to automatically discover the FastAPI app's external IP, run the `pytest` suite against it, and report success/failure.

---

## Next Steps
Once you review and approve this plan, I will begin implementing the files in the following order:
1.  `.env.example` and base manifest files.
2.  `setup_infra.sh` script.
3.  FastAPI application code and Dockerfile.
4.  Application deployment manifests and script.
