---
title: "Manage Redpanda Connect streams via REST API"
description: "Full lifecycle management of Redpanda Connect streams — list, inspect, hot-swap, delete — without the UI"
order: 1
---

# Manage Redpanda Connect streams via REST API

Redpanda Connect runs as a long-lived service on Nexus-Stack with a REST API for managing streams (the YAML-defined pipelines). If you've done the [Bluesky tutorial](/docs/tutorials/redpanda-connect/bluesky-stream/) you've already used one endpoint. This tutorial covers the full lifecycle — useful when you have several streams running, want to update one without downtime, or you're scripting deployments.

## Prerequisites

- Nexus-Stack with `redpanda-connect` and `code-server` enabled
- Familiar with the code-server terminal — see [Run curl in code-server](/docs/tutorials/code-server/terminal-curl/)
- Ideally, you've deployed at least one stream — follow [Stream Bluesky firehose into Redpanda](/docs/tutorials/redpanda-connect/bluesky-stream/) first so the examples below have something to operate on

## The endpoint

All calls go to `http://redpanda-connect:4195` from inside the Docker network (i.e. from code-server). The base path is `/streams`, with the stream ID as a path segment.

## List all streams

```bash
curl -s http://redpanda-connect:4195/streams | python3 -m json.tool
```

Output:

```json
{
  "bluesky": {
    "active": true,
    "uptime": 1523.4,
    "uptime_str": "25m23s"
  },
  "metrics-forwarder": {
    "active": true,
    "uptime": 12.1,
    "uptime_str": "12.1s"
  }
}
```

`active: true` means the stream is running. `active: false` means it's registered but stopped (or crashed).

## Inspect one stream's config

```bash
curl -s http://redpanda-connect:4195/streams/bluesky | python3 -m json.tool
```

Returns the **full YAML config** (rendered as JSON) plus runtime metadata:

```json
{
  "active": true,
  "uptime": 1523.4,
  "config": {
    "input": { "websocket": { "url": "wss://..." } },
    "pipeline": { "processors": [...] },
    "output": { "kafka": { "addresses": ["redpanda:9092"], "topic": "bluesky-posts" } }
  }
}
```

Useful when you've forgotten what a stream does, or you want to diff against your source-controlled YAML to see if it drifted.

## Create a new stream

```bash
curl -s -X POST http://redpanda-connect:4195/streams/<stream-id> \
  -H "Content-Type: application/yaml" \
  --data-binary @path/to/pipeline.yaml
```

- `<stream-id>` — you choose. Unique per Redpanda Connect instance. Good IDs are short, kebab-case, and describe the purpose (`bluesky`, `metrics-forwarder`, `orders-to-warehouse`).
- `Content-Type: application/yaml` — tells the server how to parse the body. You can also send JSON with `application/json`.
- `--data-binary @file.yaml` — the `@` makes curl read the body from a file.

Response on success: HTTP 200 with a brief JSON confirmation.

## Hot-swap a stream's config

**POST to the same ID** with new YAML. Redpanda Connect replaces the running pipeline with the new config, without restarting the process or losing connection pools:

```bash
curl -s -X POST http://redpanda-connect:4195/streams/bluesky \
  -H "Content-Type: application/yaml" \
  --data-binary @bluesky-v2.yaml
```

Old pipeline is drained, new pipeline takes over. If the new YAML has a syntax error, the old pipeline keeps running and you get an error response — failsafe.

## Update a specific field without re-sending the whole YAML

Use **PATCH** with a JSON merge patch ([RFC 7396](https://www.rfc-editor.org/rfc/rfc7396) — a partial document that gets merged into the existing one, *not* the RFC 6902 `[{op, path, value}]` array format):

```bash
curl -s -X PATCH http://redpanda-connect:4195/streams/bluesky \
  -H "Content-Type: application/json" \
  -d '{"output": {"kafka": {"topic": "bluesky-posts-v2"}}}'
```

Fields provided in the body are merged into the existing config. Fields not mentioned stay as-is. Rarely needed; usually hot-swapping the full YAML is clearer.

## Delete a stream

```bash
curl -s -X DELETE http://redpanda-connect:4195/streams/bluesky
```

The stream stops, the registration is removed. Any messages already in the destination topic stay there — delete only stops **new** messages from being produced by this stream.

Always delete a stream before tearing down the stack (if nothing else, it's a clean shutdown; WebSocket sources get a chance to close politely).

## Health checks

**Is Redpanda Connect itself up?**

```bash
curl -sI http://redpanda-connect:4195/ready
```

`200 OK` = ready to accept stream configs. `503` = not ready (usually during startup, or if a required dependency isn't available).

**Is a specific stream still active?**

```bash
curl -s http://redpanda-connect:4195/streams/bluesky | python3 -c 'import sys,json; print(json.load(sys.stdin)["active"])'
```

Prints `True` or `False`. Scriptable.

## Practical patterns

### Deploy from source control

```bash
# In your streams/ directory
for file in *.yaml; do
  id="${file%.yaml}"
  echo "Deploying $id..."
  curl -sf -X POST http://redpanda-connect:4195/streams/$id \
    -H "Content-Type: application/yaml" \
    --data-binary @"$file" || echo "  FAILED: $id"
done
```

One loop, all YAML files in a directory become streams with matching IDs. Idempotent — re-running replaces existing streams.

### Delete all streams

```bash
curl -s http://redpanda-connect:4195/streams | \
  python3 -c 'import sys,json; [print(k) for k in json.load(sys.stdin)]' | \
  xargs -I {} curl -s -X DELETE http://redpanda-connect:4195/streams/{}
```

Useful before a teardown or when you want a clean slate.

### Export current config to disk

```bash
# Export the bluesky stream config to bluesky.current.json
curl -s http://redpanda-connect:4195/streams/bluesky | python3 -m json.tool > bluesky.current.json
```

Snapshots what's running right now — good for audit / before making a change.

## What the API doesn't do

- **No authentication.** Anyone on the Docker network can create/delete streams. This is fine for our single-user Nexus-Stack setup because nobody else is on the network. Not fine for multi-tenant.
- **No versioning.** Deploying a new config replaces the old one. The old config is not kept. Keep your YAML in Git.
- **No metrics endpoint on `/streams`.** For per-stream throughput and error rates, use `/stats` (Prometheus format).

## Full API reference

[Redpanda Connect docs — HTTP component](https://docs.redpanda.com/redpanda-connect/components/http/about/).

## Next steps

- [Enable auto-create topics in Redpanda](/docs/tutorials/redpanda/auto-create-topics/) — needed before Redpanda Connect can write to a new topic
- [Stream Bluesky firehose into Redpanda](/docs/tutorials/redpanda-connect/bluesky-stream/) — concrete pipeline to practice these commands on
