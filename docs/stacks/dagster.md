---
title: "Dagster"
---

## Dagster

![Dagster](https://img.shields.io/badge/Dagster-4F43DD?logo=dagster&logoColor=white)

**Python-native data orchestration platform for building, testing, and monitoring data pipelines**

Dagster is a modern data orchestration framework built around Software-Defined Assets. Features include:
- Software-Defined Assets for declarative data pipelines
- Built-in data quality checks and freshness policies
- Schedule and sensor-based automation
- Web UI for monitoring runs, assets, and schedules
- Native Python API with type-checked configuration
- Partitioned assets for incremental processing
- Integration with dbt, Spark, Pandas, and more

| Setting | Value |
|---------|-------|
| Default Port | `3004` |
| Suggested Subdomain | `dagster` |
| Public Access | No |
| Website | [dagster.io](https://dagster.io) |
| Source | [GitHub](https://github.com/dagster-io/dagster) |

### Architecture

| Container | Purpose |
|-----------|---------|
| **dagster-webserver** | Web UI for monitoring runs, assets, and schedules |
| **dagster-daemon** | Background process for schedules, sensors, and run queuing |
| **dagster-postgres** | PostgreSQL storage backend for run history and event logs |

### Custom Build

Dagster is built from a custom Dockerfile (`python:3.11-slim` + pip install) since no official pre-built Docker images are published. The build includes:
- `dagster` (core)
- `dagster-webserver` (web UI)
- `dagster-postgres` (PostgreSQL storage)

### Adding Code Locations

To load your pipelines/assets, edit `workspace.yaml` on the server:
```yaml
load_from:
  - grpc_server:
      host: user-code
      port: 4000
```

### Credentials

Database password is auto-generated and available in Infisical under `dagster/DAGSTER_DB_PASSWORD`.
