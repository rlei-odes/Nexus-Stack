---
title: "Send your first event with a Python producer"
description: "Write a minimal Python producer that sends a single JSON event to a Redpanda topic"
order: 4
---

# Send your first event with a Python producer

This tutorial is the shortest possible path from "empty topic" to "my event is in Redpanda." About 15 lines of Python. No frameworks, no Docker, no build step.

## Prerequisites

- Nexus-Stack deployment with `redpanda`, `redpanda-console`, and `code-server` enabled
- A topic to send to — if you don't have one yet, see [Create a topic in Redpanda Console](/docs/tutorials/redpanda/create-topic/). This tutorial assumes a topic named `sensors` exists.

## Why code-server

Your producer needs to reach Redpanda's Kafka port (`9092`). That port is **not** exposed to the public internet — it's inside the Docker network of your Nexus-Stack server. The simplest way to run code inside that network is **code-server**, the VS Code running in your browser, on the server itself.

Open `https://code.<your-domain>` in the browser. If it's your first visit, Cloudflare Access will email you an OTP.

## Set up a Python environment

In code-server, open a terminal (``Ctrl+` `` or **Terminal → New Terminal**):

```bash
# Create a fresh working directory
mkdir -p ~/producer-demo && cd ~/producer-demo

# Create an isolated Python environment with uv (preinstalled on the server)
uv venv .venv
source .venv/bin/activate

# Install the Kafka client library
uv pip install confluent-kafka
```

The `confluent-kafka` library is a Python wrapper around `librdkafka`, the same C library many production Kafka clients use. It's fast, well-documented, and works identically with Redpanda (Redpanda is wire-compatible with Kafka).

## Write the producer

Create a new file `producer.py` in the same directory:

```python
from confluent_kafka import Producer
import json, time

# 1. Connect to the broker.
#    'redpanda:9092' works because code-server is on the same Docker network.
producer = Producer({'bootstrap.servers': 'redpanda:9092'})
print('Producer connected.')

# 2. Build the event.
topic = 'sensors'
key   = 'sensor-01'                      # same key → same partition (ordering preserved)
value = json.dumps({                     # the payload, as bytes on the wire
    'sensor':    'sensor-01',
    'reading':   42.5,
    'unit':      'celsius',
    'timestamp': int(time.time()),       # epoch seconds as integer — Spark/Flink schemas further down
                                         # expect BIGINT here; a float would produce NULL on strict parse
})

# 3. Send and wait for the broker to acknowledge.
def on_delivery(err, msg):
    if err:
        print(f'Delivery failed: {err}')
    else:
        print(f'Delivered -> topic={msg.topic()} partition={msg.partition()} offset={msg.offset()}')

producer.produce(topic, key=key.encode(), value=value.encode(), callback=on_delivery)
producer.flush()   # blocks until all pending messages are delivered (or error out)
print('Done.')
```

Run it:

```bash
python producer.py
```

Expected output:

```
Producer connected.
Delivered -> topic=sensors partition=0 offset=0
Done.
```

Your first event is in Redpanda.

## Verify in the Console

Open `https://redpanda-console.<your-domain>` → **Topics** → `sensors` → **Messages** tab.

You should see one row with:
- **Key:** `sensor-01`
- **Value:** the JSON payload
- **Partition:** `0` or `1` (depends on how `sensor-01` hashes)
- **Offset:** `0`

If you re-run the script, you'll see the offset tick up: `1`, `2`, `3`…

## What each piece does

- **`bootstrap.servers`**: the *entry point* into the cluster. The client connects here, then Redpanda tells it about the full cluster topology. You can list multiple hosts comma-separated for redundancy (`host1:9092,host2:9092`); on single-node Nexus-Stack, one is enough.
- **`key.encode()` / `value.encode()`**: Kafka is byte-oriented — strings must be encoded to `bytes` before sending. UTF-8 is the default encoding.
- **`on_delivery` callback**: delivery is asynchronous. `produce()` queues the message locally; the broker ack arrives later. The callback fires when the ack (or error) comes back.
- **`flush()`**: blocks until the internal queue is empty and all callbacks have fired. Without this, your script could exit before messages actually land.

## Common errors

**`_MSGTIMEDOUT`** — the broker didn't acknowledge within the timeout. Usually means the broker is reachable but unhealthy (out of disk, paused, mid-restart). Check broker status: `curl -s http://redpanda:9644/v1/status/ready`. If someone has flipped auto-create topics off (not the default — see [Toggle auto-create topics](/docs/tutorials/redpanda/auto-create-topics/)) and the topic doesn't exist, writes time out too — create the topic first or flip auto-create back on.

**`Connection refused`** or **`Failed to resolve 'redpanda:9092'`** — you're running outside the Nexus-Stack Docker network. `redpanda` is a Docker service name that only resolves inside the network. Solution: run in code-server, not on your laptop.

**Script hangs at `flush()`** — usually the same as `_MSGTIMEDOUT`: broker isn't answering. Give it 10 seconds; the callback fires with an error and flush returns.

## Next steps

- **Read your events back** with a [Python consumer](/docs/tutorials/redpanda/python-consumer/)
- **Send batches** and observe partition distribution with different keys
- **Stream continuously** from an external source — see [Stream Bluesky firehose into Redpanda](/docs/tutorials/redpanda-connect/bluesky-stream/) for an example that doesn't require writing Python at all
