---
title: "Spark Structured Streaming"
description: "Read Redpanda from Databricks Spark, parse JSON, land a Bronze Delta table"
order: 0
---

# Spark Structured Streaming tutorials

**Spark Structured Streaming** runs on the Databricks side of the architecture: your Nexus-Stack holds Redpanda, Databricks reads from it and lands results into Delta tables. These four tutorials cover the path from "I've never written a streaming query" to "my Kafka stream is persisted as a Delta table with exactly-once semantics".

Everything runs inside Databricks notebooks. You don't need Unity Catalog for the first tutorial; the Bronze Delta tutorial uses UC Volumes for checkpoint storage (fine on Free Edition).

## Suggested order

1. **[Spark Structured Streaming 101](./streaming-101)** — the `rate` source and `memory` sink, no Kafka. The mental model: source → transforms → sink → checkpoint.
2. **[Read a Redpanda topic from Spark](./read-redpanda)** — SASL auth via Databricks Secret Scope, first `readStream` against Redpanda, connectivity sanity check.
3. **[Parse JSON from a Kafka topic with a schema](./parse-json-schema)** — turn raw Kafka bytes into typed Spark columns; watermarks and windowing on event time.
4. **[Write a Kafka stream to a Bronze Delta table](./bronze-delta)** — the payoff: persistent Delta sink with checkpoint, exactly-once ingestion, queryable SQL table downstream.

## How this connects to the rest

Most tutorials use a topic named `sensors` with JSON payloads of shape `{"sensor": "...", "reading": 42.5, "timestamp": 1713...}`. You can produce events with the [Python producer from the Redpanda section](/docs/tutorials/redpanda/first-producer/), or feed from any other source.

For a Spark-specific reality check: the Bronze Delta tutorial's output is the "Bronze" layer of the medallion architecture. Silver / Gold layers are pure Spark batch or streaming on top of that table — outside the scope of this tutorial set, but natural next steps.
