---
title: "Flink"
description: "Stream processing with Flink SQL via the Dinky web IDE"
order: 0
---

# Flink tutorials

**Apache Flink** is Nexus-Stack's stream-processing engine, and **Dinky** is the web-based SQL IDE on top of it. Together they let you write SQL that runs continuously against a live event stream — windowed aggregations, multi-stream joins, stateful transforms — with proper exactly-once semantics.

The sweet spot for Flink SQL: when you'd otherwise write hundreds of lines of Python state management to get what Flink gives you in three lines.

## Tutorials

1. **[Dinky first-time setup: register your Flink cluster](./dinky-setup)** — the one-time config step that makes Dinky talk to the JobManager. Credentials from Infisical.
2. **[Query a Redpanda topic with Flink SQL](./flink-sql-on-redpanda)** — `CREATE TABLE` with the Kafka connector, continuous `SELECT`, windowed aggregation, filter-and-write-back.
3. **[Bluesky end-to-end: Redpanda Connect → Flink SQL](./bluesky-end-to-end)** — a guided full-stack walkthrough that chains the Bluesky ingest and the Flink query into one exercise.

## Task slots, the thing that trips everyone up

Nexus-Stack's Flink is configured with **2 task slots**. Each running streaming job (`INSERT INTO …`) uses one. An interactive `SELECT` whose Result tab is open also holds one. Start a third and it queues or fails.

The fix: go to Dinky → **Operations Center** → **Running Jobs** → stop anything you no longer need.

Mentioned in every tutorial in this section. Worth calling out once up front.
