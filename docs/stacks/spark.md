---
title: "Apache Spark"
---

## Apache Spark

![Spark](https://img.shields.io/badge/Apache_Spark-E25A1C?logo=apachespark&logoColor=white)

**Distributed data processing engine with standalone cluster (Master + Worker + Spark Connect)**

Apache Spark provides a unified analytics engine for large-scale data processing. This stack runs a standalone cluster with one master, one worker, and a Spark Connect server, pre-configured with Hetzner Object Storage (S3) access.

| Setting | Value |
|---------|-------|
| Default Port | `8088` (Master Web UI) |
| Classic cluster port | `7077` (internal — Jupyter connects here) |
| Spark Connect port | `15002` (internal — Marimo / future code-server connect here via `sc://`) |
| Suggested Subdomain | `spark` |
| Public Access | No (cluster management) |
| Website | [spark.apache.org](https://spark.apache.org) |
| Source | [GitHub](https://github.com/apache/spark) |

### Architecture

| Container | Image | Purpose |
|-----------|-------|---------|
| `spark-master` | `nexus-spark:4.1.1-python3.13` | Cluster manager + Web UI (port 8088); accepts classic-protocol clients on 7077 |
| `spark-worker` | `nexus-spark:4.1.1-python3.13` | Task executor (connects to master on 7077) |
| `spark-connect` | `nexus-spark:4.1.1-python3.13` | gRPC server on 15002 — driver-JVM for thin clients (Marimo, code-server). Connects to master like any other Spark application. |

> **Custom image:** The official `apache/spark:4.1.1` ships Python 3.10 (Ubuntu 22.04), but our Jupyter / spark-worker setup uses Python 3.13. PySpark requires matching Python versions between driver and executors. The custom Dockerfile installs Python 3.13 via deadsnakes PPA, adds `hadoop-aws` + AWS SDK v2 JARs for S3A filesystem support, and pre-downloads the Spark Connect server JARs (`spark-connect_2.13-4.1.1.jar`, `spark-connect-common_2.13-4.1.1.jar`) into `/opt/spark/jars/` so the Connect server starts without ivy resolution at runtime.

```
   ┌──────────────────────┐          ┌──────────────────────┐
   │  Jupyter PySpark     │          │  Marimo PySpark      │
   │  (driver-JVM local)  │          │  (gRPC client only)  │
   └──────────┬───────────┘          └──────────┬───────────┘
              │ spark://spark-master:7077       │ sc://spark-connect:15002
              │ (classic standalone)            │ (gRPC + Arrow)
              ▼                                 ▼
     ┌────────────────┐                 ┌──────────────────┐
     │  Spark Master  │                 │  Spark Connect   │
     │   UI: 8088     │ ◄───── 7077 ────│   driver-JVM     │
     │   port 7077    │                 │   port 15002     │
     └───────┬────────┘                 └─────────┬────────┘
             │                                    │
             ▼                                    │
     ┌────────────────┐                           │
     │  Spark Worker  │ ◄─── tasks (both ─────────┘
     │  (executors)   │       protocols converge here)
     └───────┬────────┘
             │ S3 (hadoop-aws)
             ▼
     ┌────────────────┐
     │ Hetzner Object │
     │ Storage (S3)   │
     └────────────────┘
```

Both protocols hit the same worker pool — applications submitted via classic 7077 and via Connect 15002 share the worker's cores and memory according to standard Spark FIFO scheduling.

### Configuration

- **Worker cores:** Configurable via `SPARK_WORKER_CORES` (default: 2)
- **Worker memory:** Configurable via `SPARK_WORKER_MEMORY` (default: 3g)
- **S3 access:** Pre-configured via `SPARK_HADOOP_fs_s3a_*` environment variables when Hetzner Object Storage credentials are available

### Resource Limits

Docker resource limits prevent Spark from consuming all server resources:

| Container | CPU Limit | Memory Limit | CPU Reserved | Memory Reserved |
|-----------|-----------|-------------|-------------|-----------------|
| `spark-master` | 1 | 1 GB | 0.25 | 256 MB |
| `spark-worker` | 2 | 4 GB | 0.5 | 512 MB |
| `spark-connect` | 1 | 1.5 GB | 0.25 | 256 MB |
| **Total** | **4** | **6.5 GB** | **1.0** | **1024 MB** |

On a cax31 (8 vCPU, 16 GB RAM) this leaves 4 CPU and ~9.5 GB RAM for other services. The `spark-connect` container holds the driver-JVM for ALL Connect clients (Marimo, future code-server) — bump its memory if multiple notebooks run heavy queries concurrently.

### Usage

1. Enable the Spark service in the Control Plane
2. Access the Master Web UI at `https://spark.YOUR_DOMAIN` to monitor the cluster
3. The Web UI shows registered workers, running applications, and completed jobs
4. Use Jupyter PySpark to submit jobs to the cluster (auto-configured)

### Connecting from Jupyter (classic protocol)

When both Spark and Jupyter are enabled, Jupyter automatically connects to the cluster on port 7077:

```python
from pyspark.sql import SparkSession
spark = SparkSession.builder \
    .master("spark://spark-master:7077") \
    .getOrCreate()

# Run a query
spark.sql("SELECT 1 as test").show()
```

> No configuration needed — `SPARK_MASTER` is automatically set to `spark://spark-master:7077` when the Spark stack is enabled. Jupyter's driver-JVM lives in the Jupyter container; the worker runs the executors.

### Connecting from Marimo (Spark Connect)

When both Spark and Marimo are enabled, Marimo talks to the `spark-connect` server at port 15002 via gRPC. The driver-JVM lives in the `spark-connect` container, NOT in Marimo — Marimo is a thin client (no JDK, no full pyspark).

```python
from pyspark.sql.connect.session import SparkSession
spark = SparkSession.builder.remote("sc://spark-connect:15002").getOrCreate()
spark.sql("SELECT 1 as test").show()
```

A pre-built helper module ships in the workspace seed at `marimo/_nexus_spark.py` — see [docs/stacks/marimo.md](./marimo.md) for the recommended pattern.

### Connect vs. Classic — when to use which

- **Classic (`spark://...:7077`)**: Jupyter today. Driver runs in the client. Full PySpark API surface, including the bits that haven't been ported to Connect yet (some RDD operations, certain UDF modes). Heavier client (needs JDK + full pyspark).
- **Connect (`sc://...:15002`)**: Marimo today, code-server in the future. Driver runs server-side in the `spark-connect` container. Thin client (gRPC + Arrow). Better fit for reactive notebooks; first-class formatter support in Marimo. Some advanced features have caveats — check the [Spark Connect compatibility matrix](https://spark.apache.org/docs/latest/spark-connect-overview.html#what-is-supported-in-spark-40) before relying on niche APIs.

Both protocols submit applications to the same `spark-master`, so they share the worker's resources.
