# =============================================================================
# Marimo - Reactive Python Notebook with Spark Connect client
# =============================================================================
# Base: ghcr.io/marimo-team/marimo:latest-sql (python:3.13-slim, includes
# marimo[sql] = duckdb + sqlglot + ibis + polars + pyarrow). We layer on
# git (so the entrypoint can clone the workspace repo from Gitea) and the
# Spark Connect client packages so notebooks can talk to the spark-connect
# service via sc://spark-connect:15002 — no JDK or full pyspark needed.
#
# Why pyspark[connect] (not full pyspark): connect mode ships the gRPC
# client + Arrow serialization only. The driver-JVM runs server-side in
# the spark-connect container, so this image stays slim (no openjdk,
# ~+40 MiB instead of ~+450 MiB for full pyspark + JDK).
#
# Why ibis-framework[pyspark]: gives us mo.sql(..., engine=con) where
# con = ibis.pyspark.connect(spark). Marimo doesn't have a first-class
# Spark SQL backend, but Ibis is the supported bridge.
#
# Pin the pyspark version exactly to the cluster's Spark version (4.1.1)
# — wire-protocol skew between Connect client and server is the #1
# footgun. Bump both together.
# =============================================================================

FROM ghcr.io/marimo-team/marimo:latest-sql

USER root

# git is needed by the entrypoint's workspace-repo clone. Marimo's base
# image is Debian (NOT Alpine) — the previous stacks/marimo/docker-compose.yml
# used `apk add` which silently failed because apk doesn't exist here;
# git was simply missing. Install via apt-get at build time so the
# entrypoint can rely on it.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Spark Connect client + Ibis adapter. `--no-cache-dir` keeps the layer
# small. Versions pinned to match spark-master 4.1.1.
RUN pip install --no-cache-dir \
    "pyspark[connect]==4.1.1" \
    "ibis-framework[pyspark]==10.5.0" \
    "grpcio>=1.62" \
    "grpcio-status>=1.62" \
    "pyarrow>=15"

USER appuser
