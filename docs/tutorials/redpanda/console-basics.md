---
title: "Redpanda Console basics"
description: "Open the Redpanda Console web UI and navigate topics, messages, partitions, and consumer groups"
order: 1
---

# Redpanda Console basics

The **Redpanda Console** is the web UI for your Redpanda broker — the place you go to see what topics exist, what's flowing through them, and which consumers are reading what. Everything in this tutorial is read-only exploration; no code is written.

## Prerequisites

- Nexus-Stack deployment running
- `redpanda` and `redpanda-console` services enabled in the Control Plane → [Stacks](/docs/guides/user-guides/stacks/) page

## Open the Console

Navigate to `https://redpanda-console.<your-domain>` in the browser. The first time, Cloudflare Access will send you an email OTP — enter the code, and you land on the Console home.

No username/password inside the Console itself: access is gated at the edge by Cloudflare, not by the Console.

## What each page is for

| Nav item | What you see there |
|---|---|
| **Overview** | Cluster health: broker count, topic count, total partitions, disk usage |
| **Topics** | List of all topics, message count per topic, disk usage per topic |
| **Schema Registry** | If you use Avro/Protobuf schemas (not required for JSON) |
| **Consumer Groups** | Every consumer group currently connected, its lag per partition, which members are assigned to which partitions |
| **Connectors** | Kafka Connect UI (only populated when a connector is running) |
| **Cluster** | Broker list, broker configuration, ACLs |

The two pages you'll use 90% of the time are **Topics** and **Consumer Groups**.

## Inspect a topic

Click any topic in the **Topics** list to drill in. You land on four tabs:

- **Messages** — live tail of events. Use the filters at the top to narrow by partition, offset, key, or timestamp. Click a message to expand its full payload and headers.
- **Partitions** — per-partition stats: start offset, end offset, message count, disk usage, leader broker. Useful when debugging why events "didn't arrive" — they did, but on a partition you weren't reading.
- **Configuration** — every Kafka config key applied to this topic. `cleanup.policy`, `retention.ms`, `max.message.bytes`, etc.
- **Consumers** — which consumer groups are currently reading from this topic.

## Inspect a consumer group

**Consumer Groups** → click a group name. You see:

- **State** — `Stable` (everyone connected, assignments final), `Rebalancing` (group membership changing), or `Empty` (no active members, offsets retained)
- **Lag per partition** — how many messages are still unread. A lag that stays large and isn't shrinking means the consumer can't keep up (or is stopped).
- **Member assignments** — which consumer process owns which partitions right now.

Lag is the single most useful number in the whole UI — it tells you instantly whether your pipeline is keeping up in real time.

## Common navigation patterns

- **"Is my producer actually sending?"** → Topics → `<topic>` → Messages tab, watch the offset column tick up.
- **"Why isn't my consumer receiving?"** → Consumer Groups → `<group>` → check lag. If lag is 0 and state is Stable, the consumer is fine — the producer isn't sending.
- **"Which partition did this event land on?"** → Topics → Messages → expand the message → `Partition` field at the top. Keys hash to partitions deterministically (same key → same partition).

## Next steps

- [Create a topic in Redpanda Console](/docs/tutorials/redpanda/create-topic/) — the next-most-common action
- [Send your first event with a Python producer](/docs/tutorials/redpanda/first-producer/) — minimal code to put data into a topic
