---
title: "MinIO"
---

## MinIO

![MinIO](https://img.shields.io/badge/MinIO-C72E49?logo=minio&logoColor=white)

**High-performance S3-compatible object storage**

MinIO is a high-performance, S3-compatible object storage system designed for large-scale data infrastructure. Features include:
- Amazon S3 API compatible
- High performance for both streaming and throughput
- Distributed mode for high availability
- Lambda-compatible event notifications
- Encryption (at rest and in transit)
- Perfect for data lakes, ML models, backups

| Setting | Value |
|---------|-------|
| Default Port | `9001` (Console), `9000` (API) |
| Suggested Subdomain | `minio` |
| Public Access | No (storage infrastructure) |
| Website | [min.io](https://min.io) |
| Source | [GitHub](https://github.com/minio/minio) |

> ✅ **Auto-configured:** Root user (admin) is automatically created during deployment. Credentials are available in Infisical.

### Usage

Access MinIO Console at `https://minio.<domain>` to:
- Create buckets
- Upload/download objects
- Manage access policies
- Configure lifecycle rules

**S3 API Access:**
- **Console UI**: `https://minio.<domain>` (accessible via Cloudflare Tunnel)
- **S3 API**: closed by default — reachable only from inside the Docker network at `minio:9000` (other in-stack containers use this). Open it externally via **Firewall** in the Control Plane (toggle `minio` → `s3-api`, restrict to your source IP, hit **Spin Up**) — then external S3 clients can connect to `http://<your-server-ip>:9000`. The S3 API is HTTP, but Nexus-Stack does not configure a Cloudflare Tunnel ingress route for port 9000 by default — the firewall opt-in is the supported external-access path.
