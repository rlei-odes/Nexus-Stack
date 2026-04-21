---
title: "Stream Bluesky firehose into Redpanda"
description: "Use Redpanda Connect to pipe the public Bluesky Jetstream WebSocket into a Kafka topic — zero code"
order: 2
---

# Stream Bluesky firehose into Redpanda

[Bluesky](https://bsky.app) publishes a public WebSocket firehose of every post on the network — ~50–100 posts per second, no API key required. With **Redpanda Connect** (a YAML-driven data pipeline tool bundled in Nexus-Stack), you can pipe that firehose into a Redpanda topic in about 20 lines of config and zero code. From there, anything that reads Kafka (Flink, Spark, a Python consumer, a notebook) can consume it.

This tutorial covers the full lifecycle: deploy the stream, verify it's running, inspect the data, stop it.

## Prerequisites

- Nexus-Stack deployment with `redpanda`, `redpanda-connect`, `redpanda-console`, and `code-server` enabled
- Familiar with the Console — see [Redpanda Console basics](/docs/tutorials/redpanda/console-basics/)

## The pipeline, in YAML

Redpanda Connect describes a pipeline in three sections: `input`, `pipeline` (optional processors), `output`. Here's the whole Bluesky stream:

```yaml
input:
  websocket:
    url: "wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post"

pipeline:
  processors:
    - mapping: |
        root.did = this.did
        root.time_us = this.time_us
        root.operation = this.commit.operation
        root.text = this.commit.record.text
        root.created_at = this.commit.record.createdAt
        root.langs = this.commit.record.langs
        root.reply_parent = this.commit.record.reply.parent.uri | null
        root.reply_root = this.commit.record.reply.root.uri | null
        root.is_reply = this.commit.record.reply != null
        root.embed = this.commit.record.embed | null
        root.facets = this.commit.record.facets | null
        root.timestamp = now()

output:
  kafka:
    addresses: ["redpanda:9092"]
    topic: bluesky-posts
```

What it does:

- **`input.websocket`** — subscribes to Bluesky's Jetstream WebSocket and emits each message as a pipeline event.
- **`pipeline.processors[0].mapping`** — flattens the deeply nested Jetstream payload into a tidy top-level record. The `|` is "fallback" — `this.commit.record.reply.parent.uri | null` returns `null` if the path doesn't exist (most posts aren't replies).
- **`output.kafka`** — writes each event to the `bluesky-posts` topic on Redpanda.

## Step 1: Save the YAML

Redpanda Connect will write to a `bluesky-posts` topic that doesn't exist yet. On Nexus-Stack's default config, `auto_create_topics_enabled` is on, so the topic is created automatically on first write — no setup needed. If you've previously flipped that off (see [Toggle auto-create topics](/docs/tutorials/redpanda/auto-create-topics/)), either flip it back on or create the topic manually first via [Create a topic in Redpanda Console](/docs/tutorials/redpanda/create-topic/).

In code-server (`https://code.<your-domain>`), open a terminal:

```bash
mkdir -p ~/bluesky-stream && cd ~/bluesky-stream
```

Create `bluesky.yaml` with the contents from the "The pipeline, in YAML" section above.

## Step 2: Deploy the stream

Redpanda Connect runs as a long-lived service on Nexus-Stack and exposes a REST API for managing streams. POST your YAML to register and start the pipeline:

```bash
curl -s -X POST http://redpanda-connect:4195/streams/bluesky \
  -H "Content-Type: application/yaml" \
  --data-binary @bluesky.yaml
```

The path segment `/streams/bluesky` is the **stream ID** — you choose it. Posting the same ID again with a different YAML replaces the config (hot swap).

## Step 3: Verify it's running

```bash
curl -s http://redpanda-connect:4195/streams | python3 -m json.tool
```

Expected output includes your stream with `"active": true`:

```json
{
  "bluesky": {
    "active": true,
    "uptime": 3.4,
    ...
  }
}
```

Open the Console → **Topics** → `bluesky-posts`. You should see the message count rising every second — real Bluesky posts, live.

Click into the topic → **Messages** tab to see individual posts with the flattened fields (`did`, `text`, `langs`, `is_reply`, etc.).

## Step 4: Stop the stream

When you're done — especially if you're about to tear down — delete the stream to stop the WebSocket connection:

```bash
curl -s -X DELETE http://redpanda-connect:4195/streams/bluesky
```

The topic keeps the messages it's already received (subject to Redpanda retention settings). Deleting the stream only stops the flow of new ones.

## What to do with the data

Once `bluesky-posts` is populated, anything that reads Kafka can consume it:

- **A Python consumer** with `confluent-kafka` — good starter exercise
- **Flink SQL via Dinky** — run windowed aggregations over the live stream
- **Spark Structured Streaming in Databricks** — persist it to a Bronze Delta table
- **A Redpanda Connect output back out** — route posts by language, filter replies, etc.

## Redpanda Connect REST API — quick reference

All calls use `http://redpanda-connect:4195` from inside code-server:

| Action | Call |
|---|---|
| List all streams | `curl -s /streams` |
| Create / replace a stream | `curl -X POST /streams/<id> -H "Content-Type: application/yaml" --data-binary @file.yaml` |
| Get one stream's config | `curl -s /streams/<id>` |
| Delete a stream | `curl -X DELETE /streams/<id>` |
| Check overall health | `curl -s /ready` |

Full API reference: [Redpanda Connect docs — HTTP](https://docs.redpanda.com/redpanda-connect/components/http/about/).

## Common issues

**Stream deploys but `bluesky-posts` stays at 0 messages** — most likely the stream crashed silently (check the Connect container logs below); alternatively, if you've flipped auto-create topics off, the topic doesn't exist and writes are rejected — either create the topic manually or flip auto-create back on.

**`connection refused` on `redpanda-connect:4195`** — you're running the `curl` outside the Docker network. Must be inside code-server.

**Stream shows `"active": false` after deploy** — YAML parse error. Check the Redpanda Connect container logs: `docker logs redpanda-connect` from the server SSH. Most often it's indentation.

**Messages arrive with `null` in reply fields** — that's expected. Most posts aren't replies; the `| null` fallback in the mapping makes those fields `null` instead of errors.

**Rate is lower than expected (~10/s instead of ~100/s)** — Bluesky Jetstream has multiple regional endpoints. If `jetstream2.us-east.bsky.network` is slow for you, try `jetstream1.us-east.bsky.network` or `jetstream2.us-west.bsky.network`.
