---
title: "Aggregate events in a Python consumer"
description: "Build a tumbling-window aggregator in plain Python — a gentle intro to stream aggregation without Flink or Spark"
order: 8
---

# Aggregate events in a Python consumer

This is a minimal **tumbling-window aggregation** in plain Python — no Flink, no Spark, no streaming framework. It's didactic: once you've written one of these by hand, the equivalent Flink SQL or Spark Structured Streaming code reads very differently (and you'll understand *why* it exists).

The scenario: events arrive on a topic `sensors` with a key (the sensor ID) and a numeric reading. Every 10 seconds, you want to know: "how many readings arrived per sensor in the last 10 seconds, and what was the average?"

## Prerequisites

- Nexus-Stack with `redpanda`, `redpanda-console`, `code-server` enabled
- A topic `sensors` exists — see [Create a topic](/docs/tutorials/redpanda/create-topic/)
- Familiar with consumer basics — see [Read events with a Python consumer](/docs/tutorials/redpanda/python-consumer/)

## Set up a steady event source

First, get events flowing into the topic. In one code-server terminal:

```bash
mkdir -p ~/agg-demo && cd ~/agg-demo
uv venv .venv && source .venv/bin/activate
uv pip install confluent-kafka
```

Create `producer.py`:

```python
from confluent_kafka import Producer
import json, time, random

producer = Producer({'bootstrap.servers': 'redpanda:9092'})
sensors = ['sensor-a', 'sensor-b', 'sensor-c']

print('Producing 3 events per second. Ctrl+C to stop.')
while True:
    for sensor in sensors:
        value = json.dumps({'sensor': sensor, 'reading': round(random.uniform(20, 30), 2)})
        producer.produce('sensors', key=sensor.encode(), value=value.encode())
    producer.poll(0)    # serve delivery callbacks
    time.sleep(1)
```

Run it — leave it running in the background:

```bash
python producer.py &
```

## Write the aggregating consumer

In the same `~/agg-demo` directory, create `aggregator.py`:

```python
from confluent_kafka import Consumer, KafkaError
import json, time
from collections import defaultdict

WINDOW_SECONDS = 10

consumer = Consumer({
    'bootstrap.servers': 'redpanda:9092',
    'group.id':          'agg-demo',
    'auto.offset.reset': 'latest',      # only aggregate new data, ignore backlog
})
consumer.subscribe(['sensors'])

# State: for each sensor, collect readings in the current window
window_start = time.time()
window_sums = defaultdict(float)
window_counts = defaultdict(int)

def emit_window(end_time):
    print(f'\n=== Window ending at {time.strftime("%H:%M:%S", time.localtime(end_time))} ===')
    for sensor in sorted(window_sums):
        count = window_counts[sensor]
        avg = window_sums[sensor] / count if count else 0
        print(f'  {sensor:10}  count={count:3}  avg={avg:.2f}')
    print()

print(f'Aggregator running, window = {WINDOW_SECONDS}s. Ctrl+C to stop.')
try:
    while True:
        # Check if the current window has closed
        now = time.time()
        if now - window_start >= WINDOW_SECONDS:
            emit_window(now)
            window_sums.clear()
            window_counts.clear()
            window_start = now

        msg = consumer.poll(timeout=0.5)
        if msg is None:
            continue
        if msg.error():
            # _PARTITION_EOF is benign (reached the end of a partition,
            # just waiting for more). Everything else — auth failures,
            # broker unreachable, topic gone — should be visible so the
            # reader isn't stuck in a silent infinite loop.
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            print(f'Consumer error: {msg.error()}')
            continue

        data = json.loads(msg.value())
        sensor = data['sensor']
        reading = data['reading']

        window_sums[sensor] += reading
        window_counts[sensor] += 1
finally:
    consumer.close()
```

Run it in another terminal:

```bash
source .venv/bin/activate
python aggregator.py
```

Expected output (every 10 seconds):

```
Aggregator running, window = 10s. Ctrl+C to stop.

=== Window ending at 14:32:10 ===
  sensor-a    count= 10  avg=25.13
  sensor-b    count= 10  avg=24.87
  sensor-c    count= 10  avg=25.42

=== Window ending at 14:32:20 ===
  sensor-a    count= 10  avg=24.98
...
```

## What you just built

### Tumbling windows

The aggregator buckets events into **non-overlapping** 10-second windows: `[0–10s)`, `[10–20s)`, `[20–30s)`. Each event belongs to exactly one window.

Contrast with **sliding windows** (e.g. "last 10 seconds, updated every 2 seconds") where events belong to multiple overlapping windows. Sliding is more code; tumbling is the easy one.

### Processing time

The code above uses `time.time()` — **the consumer's wall clock at processing time**. Whatever events happen to arrive in a 10-second consumer-side interval get grouped together.

This is the simplest model. It's also the weakest: if the consumer is slow or restarts, events that should belong to an earlier window get lumped into a later one.

The alternative is **event time** — use the timestamp embedded *in* the message (or Kafka's record timestamp). Much more robust, much more code. It's what Flink and Spark use by default, and it's where frameworks start pulling their weight.

### State

`window_sums` and `window_counts` live in Python memory. If the consumer crashes and restarts mid-window, that state is lost. You'd also lose everything after the last committed offset unless `enable.auto.commit` is off and you control commits.

Production-grade aggregation requires either:
- Checkpointed state (what Flink/Spark do automatically)
- Writing partial results to an external store (Redis, Postgres) after every update

For this tutorial, in-memory is fine.

## Why this maps cleanly to Flink SQL

The same 10-second aggregation in Flink SQL is three lines:

```sql
SELECT sensor, COUNT(*) AS count, AVG(reading) AS avg
FROM sensors
GROUP BY sensor, TUMBLE(proc_time, INTERVAL '10' SECOND);
```

Flink handles: windowing, state persistence, exactly-once semantics, late-arriving events, scaling across machines. All things the Python version handwaves.

The didactic value of doing this by hand: next time you see `GROUP BY TUMBLE(...)` you know exactly what it's doing — and what it's saving you from.

## Cleanup

Stop the producer:

```bash
kill %1
```

(Or the job number of your producer — check with `jobs`.)

## Next steps

- [Flink SQL on a Redpanda topic](/docs/tutorials/flink/flink-sql-on-redpanda/) — the framework version of this exact pattern
- [Spark Structured Streaming 101](/docs/tutorials/spark/streaming-101/) — another framework with windowing support
