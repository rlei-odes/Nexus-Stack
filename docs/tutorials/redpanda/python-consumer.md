---
title: "Read events with a Python consumer"
description: "Write a minimal Python consumer that reads events from a Redpanda topic, the companion to the producer tutorial"
order: 5
---

# Read events with a Python consumer

The companion to [Send your first event with a Python producer](/docs/tutorials/redpanda/first-producer/). Once you have events in a topic, a **consumer** is the program that reads them back. This tutorial covers the minimal consumer loop, consumer groups, and the offset semantics that trip up most people on day one.

## Prerequisites

- Nexus-Stack with `redpanda` and `code-server` enabled
- A topic with some messages in it — produce a few with [the producer tutorial](/docs/tutorials/redpanda/first-producer/) first
- Python environment set up — see [Python venv with uv](/docs/tutorials/code-server/python-uv/)

## Set up the environment

In code-server, reuse the `~/producer-demo` from the producer tutorial, or create a fresh one:

```bash
mkdir -p ~/consumer-demo && cd ~/consumer-demo
uv venv .venv && source .venv/bin/activate
uv pip install confluent-kafka
```

## Write the consumer

Create `consumer.py`:

```python
from confluent_kafka import Consumer, KafkaError

# 1. Configure the consumer.
conf = {
    'bootstrap.servers': 'redpanda:9092',
    'group.id':          'demo-consumer',    # consumer group identifier
    'auto.offset.reset': 'earliest',         # where to start if no offset is committed yet
}
consumer = Consumer(conf)
consumer.subscribe(['sensors'])

print('Consumer subscribed — polling for messages...')

# 2. Poll loop.
try:
    while True:
        msg = consumer.poll(timeout=1.0)         # wait up to 1s for a message
        if msg is None:
            continue                             # no message this cycle
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue                         # reached end of partition, keep waiting
            print(f'Error: {msg.error()}')
            continue

        # 3. Process the message.
        #    key can be None (producers can omit it), value can be None
        #    for "tombstone" records used in log-compacted topics. Guard
        #    both before decoding so the loop doesn't crash on real topics.
        key   = msg.key().decode()   if msg.key()   is not None else '<null>'
        value = msg.value().decode() if msg.value() is not None else '<null>'
        print(f'[{msg.topic()} p{msg.partition()} o{msg.offset()}] '
              f'key={key} value={value}')
finally:
    consumer.close()
```

Run it:

```bash
python consumer.py
```

You'll see one line per message already in the topic, then the script blocks waiting for more. Produce another event (in a second terminal, running the producer from the earlier tutorial) — it shows up in the consumer's output within a second. `Ctrl+C` to stop.

## The three config keys that matter

### `group.id`

Every consumer belongs to a **consumer group**, identified by this string. Redpanda tracks the current read position (**offset**) per group, per partition. This is how you can:

- **Stop a consumer and resume** without losing your place → same `group.id`, offsets are persisted server-side
- **Split reading across multiple processes** → start N consumers with the same `group.id`, Redpanda distributes partitions among them
- **Replay from scratch** → use a new `group.id` (e.g. `demo-consumer-v2`), no committed offset exists, starts wherever `auto.offset.reset` says

### `auto.offset.reset`

What to do when the group has **no committed offset** for a partition (first read, or a new `group.id`):

- **`earliest`** — read from the oldest message still in retention. Good for development and replay.
- **`latest`** — read only new messages that arrive after the consumer starts. Good for "only show me events from now on" use cases.

This setting has **no effect** once the group has committed an offset. Redpanda uses the committed offset. Changing `auto.offset.reset` after the first run does nothing unless you also wipe the committed offsets (new `group.id` is easier).

### `enable.auto.commit` (defaults to `true`)

Not in the code above — we accept the default. By default, the client commits offsets every 5 seconds in the background. Fine for most cases.

For exactly-once or at-least-once semantics where you must control when an offset is committed, set `enable.auto.commit: false` and call `consumer.commit(msg)` manually after processing each message.

## Running two consumers in the same group

Start the consumer above in one terminal. In a second terminal, run it again — same script, same `group.id`.

On the Redpanda Console → **Consumer Groups** → `demo-consumer`, you'll see both members listed and partitions split between them. One member owns partition `0`, the other owns partition `1` (if your topic has 2 partitions).

Stop one — the other takes over its partitions within a few seconds (**rebalancing**).

## Resetting a consumer group's offsets

If you want to re-read all messages from the beginning without a new `group.id`, use Redpanda Console → **Consumer Groups** → `<group>` → **Reset offsets** button. You can reset to earliest, latest, or a specific offset per partition.

Or via the admin API in a code-server terminal:

```bash
# Reset all partitions of "sensors" for group "demo-consumer" to offset 0
rpk group seek demo-consumer --to start --topic sensors
```

(`rpk` is Redpanda's CLI, available inside the `redpanda` container: `docker exec redpanda rpk ...`.)

## Common issues

**Consumer starts but sees no messages** — either the topic is genuinely empty, or `auto.offset.reset=latest` and the existing messages are older than your consumer. Check the Console → topic → **Messages** tab.

**Two consumers in the same group, but only one receives messages** — expected if your topic has only 1 partition. A partition is owned by one consumer at a time within a group. Split across multiple consumers requires multiple partitions.

**Messages appear out of order** — order is guaranteed **within a partition**, not across. If you have 2 partitions and a key that distributes events across both, consumer output can interleave. For strict ordering, route all related events to the same partition via the same message key.

**`poll()` blocks forever** — check broker connectivity: `curl -sI http://redpanda:9644/v1/status/ready` should return `200`. If that fails, the broker isn't reachable from your container.

## Next steps

- [Inspect consumer groups and lag in the Console](/docs/tutorials/redpanda/consumer-groups-lag/) — how to tell if a consumer is keeping up
- [Partitions & keys hands-on](/docs/tutorials/redpanda/partitions-keys/) — what determines which partition a message lands on
- [Aggregate events in a consumer](/docs/tutorials/redpanda/consumer-aggregation/) — a tumbling-window aggregation in Python
