---
title: "Inspect consumer groups and lag in the Console"
description: "Read the Consumer Groups view in Redpanda Console — what Stable, Rebalancing, and Lag actually mean"
order: 6
---

# Inspect consumer groups and lag in the Console

**Lag** is the single most useful number when debugging a streaming pipeline. It answers: *"is my consumer keeping up with the producer, or falling behind?"* This tutorial walks through the Consumer Groups page in Redpanda Console and teaches you to read it confidently.

## Prerequisites

- Nexus-Stack with `redpanda` and `redpanda-console` enabled
- At least one consumer group currently exists — run the [Python consumer tutorial](/docs/tutorials/redpanda/python-consumer/) first to create one

## Open the view

`https://redpanda-console.<your-domain>` → **Consumer Groups** in the left nav.

You see a table with one row per group:

| Column | Meaning |
|---|---|
| **Group ID** | The `group.id` config value your consumer connected with |
| **State** | `Stable`, `Rebalancing`, `Empty`, `Dead`, `PreparingRebalance`, or `CompletingRebalance` |
| **Protocol** | Usually `consumer` (the standard Kafka consumer protocol) |
| **Members** | How many consumer processes are currently connected |
| **Lag (sum)** | Total unread messages across all partitions this group subscribes to |

Click a group name to drill into its detail page.

## The states, in plain language

- **`Stable`** — everyone connected, partition assignments finalised, reading. The happy path.
- **`Empty`** — nobody is currently connected to this group, but the group (and its committed offsets) still exists. Reconnecting with the same `group.id` picks up where the last consumer left off.
- **`Rebalancing`** (and `PreparingRebalance`, `CompletingRebalance`) — the group membership is changing (a consumer joined, left, or timed out). During this phase, consumers aren't processing messages. Usually resolves in <10 seconds.
- **`Dead`** — the group was deleted (or its coordinator crashed). Rare.

If a group stays `Rebalancing` for more than 30 seconds, something's wrong — typically a consumer that crashed without a clean disconnect, or a `session.timeout.ms` misconfiguration.

## The group detail page

Click a group row. Four sections:

### Assignments

A table of which **member** (consumer process) currently owns which **partitions**:

| Member ID | Client ID | Host | Assignments |
|---|---|---|---|
| `rdkafka-...-abc` | `rdkafka` | `10.0.1.23` | `sensors:[0,1]` |

If you have 2 partitions and 2 consumers, you'd see one partition per row. If you have 2 partitions and 1 consumer, one member owns both.

### Lag per partition

The most important view. Columns:

- **Topic / Partition** — `sensors-0`, `sensors-1`, etc.
- **Current Offset** — the last offset this group has **committed** (= confirmed it processed)
- **Log End Offset** — the **latest** offset in that partition (= where the producer is writing right now)
- **Lag** — `Log End Offset - Current Offset`. Unread messages still waiting.

**Examples:**

| Current | End | Lag | Interpretation |
|---|---|---|---|
| 1250 | 1250 | 0 | Consumer is caught up in real time |
| 1150 | 1250 | 100 | Consumer is 100 messages behind — probably catching up |
| 500 | 50000 | 49500 | Consumer is way behind — either just started with `earliest`, or it can't keep up |
| 800 | 800 | 0 | Caught up this instant |
| 900 | 1000 | 100 | A moment later, producer is writing faster than consumer is reading |

### Offsets per partition

Same data as "Lag per partition" but focused on the offset numbers themselves. Useful for answering "what offset is my consumer at right now?"

### Reset offsets

A UI button that lets you move the committed offset for this group to:
- **Earliest** (read everything again)
- **Latest** (skip everything, only read from now)
- **Specific offset** (per partition)
- **Specific timestamp** (per partition)

Only works when the group is `Empty` — all consumers must disconnect first. Otherwise: "group is not empty" error.

## What "lag is fine" actually looks like

For a **real-time streaming pipeline**: lag stays **close to zero** (< 100 messages) and doesn't grow over time. Brief spikes during peak load that drain quickly are normal.

For a **batch-style consumer** that polls every N minutes: lag grows steadily between polls, then drops to ~0 after each poll. This is fine as long as it drops each cycle.

For a **just-started consumer** with `auto.offset.reset=earliest` on an existing topic: lag is huge initially (matching the whole topic's message count) and drains as the consumer catches up. Watch the rate of decrease — if it's not dropping, the consumer can't keep up with even the historical backlog.

## Debugging checklist

### Lag is growing and not shrinking

- **Producer rate > consumer rate.** Either add more consumer instances (up to the partition count — more consumers than partitions doesn't help), or make the consumer faster.
- **Consumer is stuck on a bad message.** Check consumer logs for exceptions in the processing loop.
- **Consumer is slow to `commit()`** — if using manual commits, offsets get committed less often than messages are read, so displayed lag can be misleadingly high. Correlate with actual processing throughput.

### Lag is 0 but consumer output is empty

- Producer isn't producing. Check the topic's **Messages** tab — no new offsets = no new messages arriving.

### Group state stuck on `Rebalancing`

- A consumer crashed without a clean disconnect. Wait ~30s for the session to time out and the group to rebalance without it. If still stuck, restart all group members.

### Group doesn't appear in the list

- No consumer has ever connected with that `group.id`. Groups are lazy-created on first connect.

### Lag is negative

- Very rare, indicates a clock-skew or off-by-one issue. Usually safe to ignore if it self-corrects within a few seconds.

## From the CLI

The same info is available via `rpk` (Redpanda's CLI) inside the Redpanda container:

```bash
docker exec redpanda rpk group describe demo-consumer
```

Output includes all members, lag, offsets, and state — same as the UI, in one screenful of text.

## Next steps

- [Partitions & keys hands-on](/docs/tutorials/redpanda/partitions-keys/) — understand which partition a message lands on
- [Aggregate events in a consumer](/docs/tutorials/redpanda/consumer-aggregation/) — a Python consumer that processes windowed aggregates
