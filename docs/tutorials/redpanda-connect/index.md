---
title: "Redpanda Connect"
description: "YAML-driven data pipelines — ingest external streams into Redpanda with zero code"
order: 0
---

# Redpanda Connect tutorials

**Redpanda Connect** is a data-pipeline tool bundled with Nexus-Stack. You describe an `input → processor → output` chain in YAML, POST it to the REST API, and the pipeline runs as a long-lived stream. No Python, no Docker, no deployment step.

Most useful when you need to pull data from somewhere external (a WebSocket, an HTTP API, S3, another Kafka cluster) and land it in a Redpanda topic where other tools — Flink, Spark, your own Python consumer — can pick it up.

## Tutorials

1. **[Manage Redpanda Connect streams via REST API](./manage-streams)** — stream lifecycle: deploy, inspect, hot-swap, delete. The operational reference.
2. **[Stream Bluesky firehose into Redpanda](./bluesky-stream)** — concrete use case: the public Bluesky Jetstream piped into a `bluesky-posts` topic with ~20 lines of YAML.

## When to reach for Redpanda Connect

- **External data lands in Redpanda:** it's what Connect is designed for — WebSockets, HTTP pulls, S3 listings, other Kafkas, SQL change-data-capture.
- **Simple fan-out / filter:** "route all English posts to their own topic" — can be done without spinning up Flink.
- **When to use Flink instead:** windowed aggregations, multi-stream joins, complex stateful logic. See [Flink](/docs/tutorials/flink/).
- **When to use Spark instead:** you want the data in a Delta table inside Databricks. See [Spark](/docs/tutorials/spark/).
