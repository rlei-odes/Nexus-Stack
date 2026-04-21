---
title: "Create a topic in Redpanda Console"
description: "Create a Kafka topic through the Redpanda Console UI and understand partitions and replication factor"
order: 2
---

# Create a topic in Redpanda Console

A **topic** is a named stream of events — think of it as a table that's append-only and ordered. Before a producer can write or a consumer can read, the topic has to exist. This tutorial creates one via the Console UI.

## Prerequisites

- Nexus-Stack deployment running with `redpanda` and `redpanda-console` enabled
- Familiar with the Console layout — see [Redpanda Console basics](/docs/tutorials/redpanda/console-basics/)

## Create the topic

1. Open `https://redpanda-console.<your-domain>`
2. **Topics** → **Create Topic** (top right)
3. Fill in:
   - **Name:** whatever you want (e.g. `sensors`). Topic names should be short, descriptive, and lowercase. Hyphens are fine, dots are not idiomatic.
   - **Partitions:** `2` for a playground topic. More on this below.
   - **Replication Factor:** `1`. You'll hit an error if you try more — our single-node Redpanda has only one broker.
   - Leave the rest on their defaults.
4. **Create**

The topic appears in the Topics list immediately. Messages: 0, disk usage: 0 B.

## What you just chose

### Partitions

A partition is a single ordered log. A topic is physically a collection of partitions — events are distributed across them based on the message key (same key always lands on the same partition).

**Why more than one?** Parallelism on the consumer side. A consumer group with N members can read from up to N partitions in parallel, each member owning some partitions exclusively. One partition = one consumer can read it at a time.

**Why not a huge number?** Every partition has overhead (file handles, memory, metadata). For a playground topic with one or two producers, 2 is plenty. Production workloads typically start at 6–12.

**Can you change it later?** You can *increase* partitions on an existing topic, but not decrease — and increasing reshuffles key-to-partition mappings, which breaks consumers that rely on per-key ordering. Pick once, pick well.

### Replication factor

How many brokers hold a copy of each partition. `1` means no redundancy — if the broker dies, the data is gone. Production clusters use `3` for fault tolerance.

Our single-node Redpanda in Nexus-Stack only supports `1`. That's fine for development and learning; don't rely on it for anything you can't afford to lose.

### The defaults you didn't touch

Worth knowing exist:

- **`cleanup.policy`** = `delete` — old messages get deleted after retention expires
- **`retention.ms`** = 1 week by default — messages older than this are eligible for deletion
- **`max.message.bytes`** = 1 MiB — single event size limit
- **`compression.type`** = `producer` — compression is decided by the producer, broker accepts whatever comes in

All visible (and editable) on the topic's **Configuration** tab after creation.

## Verify

Click the new topic in the Topics list:

- **Partitions** tab: you should see exactly 2 rows, offsets all 0
- **Configuration** tab: `cleanup.policy=delete`, `retention.ms=604800000` (one week in ms)
- **Messages** tab: empty, with a note about "No messages found in the selected time range"

Next: [send your first event into it](/docs/tutorials/redpanda/first-producer/).

## Common mistakes

- **Replication factor > broker count** → "not enough replicas" error. Use `1` on Nexus-Stack.
- **Topic name with dots and underscores** (e.g. `my.sensors_v2`) — legal, but Kafka has known issues with mixing both in the same name. Stick to hyphens if you need word separators.
- **Deleting a topic to "reset" it** — works, but in-flight consumers will error until they're restarted. In a playground this is fine; know that deletion is aggressive.

## Creating topics from code

Topics auto-create on first `producer.produce()` when the cluster has `auto_create_topics_enabled=true`. On Nexus-Stack's default Redpanda config, this is **on** (see `stacks/redpanda/config/redpanda.yaml`), so a producer writing to a non-existent topic will quietly create it with default settings (1 partition, replication factor 1, 1-week retention).

Useful for prototyping, risky for production — typos silently create stray topics instead of failing loudly. If you want explicit topic creation only, see [Toggle auto-create topics in Redpanda](/docs/tutorials/redpanda/auto-create-topics/) for the admin-API call to flip it off.
