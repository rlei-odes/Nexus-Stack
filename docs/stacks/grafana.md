---
title: "Grafana"
---

## Grafana

![Grafana](https://img.shields.io/badge/Grafana-F46800?logo=grafana&logoColor=white)

**Full observability stack with Prometheus, Loki & dashboards**

A complete monitoring and observability solution including:
- **Grafana** - Beautiful dashboards and visualization
- **Prometheus** - Metrics collection and alerting
- **Loki** - Log aggregation (like Prometheus, but for logs)
- **Promtail** - Ships Docker container logs to Loki
- **cAdvisor** - Container metrics (CPU, memory, network, disk)
- **Node Exporter** - Host-level metrics (CPU, RAM, disk, network)

| Setting | Value |
|---------|-------|
| Default Port | `3100` (→ internal 3000) |
| Suggested Subdomain | `grafana` |
| Public Access | **Never** (always protected) |
| Website | [grafana.com](https://grafana.com) |
| Source | [GitHub](https://github.com/grafana/grafana) |

### Pre-configured Dashboards

The stack comes with three ready-to-use dashboards:

| Dashboard | Description |
|-----------|-------------|
| **Docker Overview** | Container CPU, memory, network I/O, and disk usage |
| **Loki Logs** | Real-time log viewing and filtering for all containers |
| **Node Exporter** | Host metrics including CPU, memory, disk, and network |

### Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Containers │────▶│  Promtail   │────▶│    Loki     │
│   (logs)    │     │             │     │             │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
┌─────────────┐     ┌─────────────┐            │
│  cAdvisor   │────▶│ Prometheus  │            │
│  (metrics)  │     │             │────────────┼──────▶ Grafana
└─────────────┘     └──────┬──────┘            │
                           │                   │
┌─────────────┐            │                   │
│Node Exporter│────────────┘                   │
│(host stats) │                                │
└─────────────┘                                │
```

> ✅ **Auto-configured:** Admin password is set via environment variables during deployment. Dashboards and datasources are pre-provisioned. Credentials are available in Infisical.
