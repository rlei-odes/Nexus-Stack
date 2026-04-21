---
title: "Redpanda"
description: "Kafka-compatible event streaming — topics, producers, consumers, partitions, consumer groups"
order: 0
---

# Redpanda tutorials

**Redpanda** is Nexus-Stack's Kafka-compatible message broker. Everything here is Kafka-native: `confluent-kafka` Python clients, `rpk` CLI, the Console UI — they all work identically against vanilla Kafka, and vice versa.

These eight tutorials cover the full "producer/consumer/topic" mental model end to end. They build on each other, but each one is self-contained.

## Suggested path for newcomers

### The fundamentals

1. **[Redpanda Console basics](./console-basics)** — UI walkthrough: topics, messages, partitions, consumer groups.
2. **[Create a topic](./create-topic)** — partitions, replication factor, and what the defaults actually do.
3. **[Toggle auto-create topics](./auto-create-topics)** — one curl call to flip cluster-wide auto-create on or off.

### Producing and consuming

4. **[Send your first event with a Python producer](./first-producer)** — the minimal 15-line Python script that writes a single JSON event.
5. **[Read events with a Python consumer](./python-consumer)** — the companion: consumer groups, offsets, and `poll()` loop.
6. **[Inspect consumer groups and lag](./consumer-groups-lag)** — diagnosis: how to tell if a consumer is keeping up.

### Beyond the basics

7. **[Partitions and keys, hands-on](./partitions-keys)** — see for yourself how the same key always lands on the same partition.
8. **[Aggregate events in a Python consumer](./consumer-aggregation)** — a tumbling-window aggregation in plain Python, a didactic warm-up before Flink / Spark.

## What's next

- Want to pipe an external stream in without writing code? → [Redpanda Connect](/docs/tutorials/redpanda-connect/)
- Want to run SQL against a Redpanda topic? → [Flink SQL in Dinky](/docs/tutorials/flink/)
- Want to land it in a lakehouse? → [Spark Structured Streaming](/docs/tutorials/spark/)
