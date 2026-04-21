---
title: "Partitions and keys, hands-on"
description: "See how message keys determine partition assignment — and what happens when you leave the key empty"
order: 7
---

# Partitions and keys, hands-on

The single most important rule of Kafka/Redpanda: **messages with the same key always land on the same partition.** This tutorial is a 5-minute experiment to make that concrete — you'll write a batch producer, look at the Console, and watch keys map to partitions.

## Prerequisites

- Nexus-Stack with `redpanda`, `redpanda-console`, and `code-server` enabled
- Python environment set up — see [Python venv with uv](/docs/tutorials/code-server/python-uv/)
- A topic with **at least 2 partitions** — create one named `keys-demo` with 4 partitions to see the distribution clearly. Via the Console, or:

```bash
docker exec redpanda rpk topic create keys-demo --partitions 4 --replicas 1
```

## The experiment

Create `batch.py`:

```python
from confluent_kafka import Producer
import json, time

producer = Producer({'bootstrap.servers': 'redpanda:9092'})

# Three "houses" — each will be a key. Watch where they end up.
houses = ['haus_a', 'haus_b', 'haus_c']

def ack(err, msg):
    if err:
        print(f'FAILED: {err}')
    else:
        # Key is optional — guard for the "What happens without a key" experiment
        # further down where we'd otherwise dereference None.
        key = msg.key().decode() if msg.key() is not None else '<none>'
        print(f'key={key:10} -> partition {msg.partition()} offset {msg.offset()}')

# Send 6 messages per house = 18 total
for i in range(6):
    for house in houses:
        value = json.dumps({'house': house, 'reading': i, 'timestamp': int(time.time())})
        producer.produce(
            'keys-demo',
            key=house.encode(),
            value=value.encode(),
            callback=ack,
        )

producer.flush()
```

Run:

```bash
python batch.py
```

Output (partition numbers will vary based on hashing, but pattern is stable):

```
key=haus_a     -> partition 3 offset 0
key=haus_b     -> partition 1 offset 0
key=haus_c     -> partition 2 offset 0
key=haus_a     -> partition 3 offset 1
key=haus_b     -> partition 1 offset 1
key=haus_c     -> partition 2 offset 1
...
```

**Observe:** every `haus_a` lands on partition 3, every `haus_b` on partition 1, every `haus_c` on partition 2. Deterministic. Rerun the script — same mapping.

## Verify in the Console

`https://redpanda-console.<your-domain>` → **Topics** → `keys-demo` → **Partitions** tab.

You should see:
- One partition with 0 messages (the one none of our keys hashed to)
- Three partitions with 6 messages each (one key per partition)

Click into one of the populated partitions → **Messages** → every message has the same key. This is the guarantee: same key → same partition.

## Why this matters

**Ordering.** Kafka guarantees order **within a partition**, not across. If you need all `haus_a` events to be processed in the order they were sent, they **must** all live in the same partition. The way to ensure that: give them all the same key.

Real-world examples:

- **User session events** — key by `user_id`. All events for one user stay in order.
- **Sensor readings** — key by `sensor_id`. Consumer aggregating per sensor sees readings in chronological order.
- **Orders by account** — key by `account_id`. State machines stay consistent per account.

## What happens without a key

Modify the script: remove `key=house.encode()` from the `produce()` call. Produce 12 messages:

```python
for i in range(12):
    producer.produce('keys-demo', value=json.dumps({'n': i}).encode(), callback=ack)
```

Output (typical):

```
key=<none>     -> partition 0 offset 0
key=<none>     -> partition 1 offset 12
key=<none>     -> partition 2 offset 6
key=<none>     -> partition 3 offset 6
key=<none>     -> partition 0 offset 1
...
```

With no key, Redpanda's producer uses **sticky partitioning** — it picks one partition and sends a batch there, then picks another, and so on. This is good for throughput (bigger batches = fewer round trips), but it means ordering is lost across partitions. Perfectly fine for "fire and forget" logs where order doesn't matter.

## The hashing function

`confluent-kafka` uses **murmur2** (Java Kafka's default) to hash keys to partition indices:

```
partition = murmur2(key) % num_partitions
```

Key implications:

- **Same key, same topic, same partition count → same partition, always.** No per-producer state.
- **Change the partition count → all mappings change.** That's why you shouldn't increase partitions on a live topic that relies on per-key ordering — keys will re-route, and your consumer might temporarily see out-of-order events.
- **Different client libraries sometimes hash differently.** Most now default to murmur2 for Kafka compatibility, but double-check if you're bridging Java and Python producers to the same topic.

## Key skew and hot partitions

If your keys are unevenly distributed (e.g. 90% of your traffic has `user_id = 12345`), the partition that key hashes to gets 90% of the load. Other partitions idle. This is **key skew**, and it defeats the scaling benefit of partitioning.

**Solutions:**
- **High-cardinality keys** — many distinct values spread the load naturally.
- **Compound keys** — `user_id + random_suffix` for log-style data where you don't need strict per-user ordering.
- **Custom partitioner** — supply your own partition-selection function in the producer config. Rare; usually a smell.

## Inspecting key distribution

Quick check via `rpk`:

```bash
docker exec redpanda rpk topic describe keys-demo -p
```

Shows message count per partition. Wide variance = skew.

## Next steps

- [Aggregate events in a consumer](/docs/tutorials/redpanda/consumer-aggregation/) — uses keys to group aggregates per entity
- [Inspect consumer groups and lag](/docs/tutorials/redpanda/consumer-groups-lag/) — lag is per-partition, so skew shows up there too
