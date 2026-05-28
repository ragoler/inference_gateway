# Inference Gateway on GKE with FastAPI Application

This repository automates the provisioning, configuration, and deployment of a Google Kubernetes Engine (GKE) cluster configured with the Kubernetes Gateway API Inference Extension. It routes LLM inference traffic to a CPU-based vLLM model server pool and provides a production-ready FastAPI consumer application.

---

## 🏗️ Architecture Overview

The system architecture is divided into three primary layers:

1. **Infrastructure Tier**: A dedicated GKE Cluster provisioned with the standard Kubernetes Gateway API.
2. **Inference Tier**:
   - **L7 Managed External Gateway** (`gke-l7-regional-external-managed`).
   - **Endpoint Picker (EPP) Extension**: Optimally balances requests across inference pools.
   - **vLLM CPU Model Servers**: Running high-performance serving for HuggingFace models (e.g., `Qwen/Qwen2.5-1.5B-Instruct`).
3. **Application Tier**:
   - A fully containerized Python FastAPI microservice running inside the cluster.
   - Exposes clean REST APIs (`/generate`) and seamlessly interacts with the backend Inference Gateway.

---

## 📁 Repository Structure

```
├── .env.example             # Template for configuration environment variables
├── app/
│   ├── Dockerfile           # Multi-stage containerization for the FastAPI service
│   ├── main.py              # FastAPI server handling prompt formatting & routing
│   └── requirements.txt     # Python runtime dependencies
├── infra/
│   ├── cpu-deployment.yaml  # vLLM model server configuration & resources
│   └── gateway.yaml         # External L7 Gateway definition
├── k8s/
│   ├── app-deployment.yaml  # FastAPI application Deployment manifest
│   └── app-service.yaml     # FastAPI external LoadBalancer Service
├── tests/
│   └── test_integration.py  # End-to-end automated testing suite
├── setup_infra.sh           # Script to provision GKE, CRDs, and inference pools
├── deploy_app.sh            # Script to build, push, and deploy the FastAPI container
└── verify_setup.sh          # Post-deployment validation & integration testing launcher
```

---

## 🚀 Getting Started

### 1. Environment Configuration
Create a `.env` file based on the provided template:
```bash
cp .env.example .env
```
Edit `.env` to configure your specific GCP project, cluster name, zones, machine types, and model options.

### 2. Provision the GKE Cluster & Gateway API
Execute the infrastructure setup script to provision your GKE cluster, enable Gateway API CRDs, and deploy the vLLM model servers:
```bash
./setup_infra.sh
```

### 3. Build and Deploy the FastAPI Microservice
Once the cluster and gateway are healthy, deploy the backend application:
```bash
./deploy_app.sh
```

### 4. Verify and Test
Automatically discover the external LoadBalancer IP and validate the entire end-to-end generative AI workflow:
```bash
./verify_setup.sh
```