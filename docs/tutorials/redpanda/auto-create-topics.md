---
title: "Toggle auto-create topics in Redpanda"
description: "Flip cluster.auto_create_topics_enabled on or off via the admin API — one curl call"
order: 3
---

# Toggle auto-create topics in Redpanda

Redpanda has a cluster-wide setting that decides what happens when a producer writes to a topic that doesn't exist yet: either the topic is quietly created with default settings, or the write is rejected. On Nexus-Stack this is **on** by default (see `stacks/redpanda/config/redpanda.yaml`) — convenient for prototyping and streaming pipelines that create topics on the fly. You may want to flip it off in stricter environments where implicit creation hides typos and drift. This tutorial is the one curl call either way.

## Prerequisites

- Nexus-Stack with `redpanda` and `code-server` enabled
- Familiar with the code-server terminal — see [Run curl in the code-server terminal](/docs/tutorials/code-server/terminal-curl/)

## Check the current state

In a code-server terminal:

```bash
curl -s http://redpanda:9644/v1/cluster_config | python3 -m json.tool | grep auto_create_topics
```

Expected on a stock Nexus-Stack:

```
    "auto_create_topics_enabled": true,
```

## Flip it off (strict mode)

```bash
curl -s -X PUT http://redpanda:9644/v1/cluster_config \
  -H "Content-Type: application/json" \
  -d '{"upsert": {"auto_create_topics_enabled": false}, "remove": []}'
```

Expected response:

```json
{"config_version":N}
```

(`N` is some number — the config revision increments every time you change cluster config.)

From now on, producers writing to a non-existent topic get rejected instead of silently creating a new one. You have to create topics explicitly — see [Create a topic in Redpanda Console](/docs/tutorials/redpanda/create-topic/).

## Flip it on (permissive mode — the Nexus-Stack default)

Symmetric call:

```bash
curl -s -X PUT http://redpanda:9644/v1/cluster_config \
  -H "Content-Type: application/json" \
  -d '{"upsert": {"auto_create_topics_enabled": true}, "remove": []}'
```

After a fresh spin-up this is already `true`, so this call is only needed if you previously flipped it off.

## Why this is cluster config, not topic config

`auto_create_topics_enabled` is a **broker-level** setting — it affects the cluster as a whole, not individual topics. That's why you set it via the cluster config endpoint (`/v1/cluster_config`) and not the topics endpoint.

## What gets created when auto-creation fires

The auto-created topic uses Redpanda's **default-topic** config values:
- **Partitions:** `default_topic_partitions` (1 on Nexus-Stack)
- **Replication factor:** `default_topic_replications` (1 — can't be higher on single-node)
- **Retention:** `log_retention_ms` (1 week)
- **Cleanup policy:** `cleanup_policy` (`delete`)

If you want different values for a specific topic, either:
- Create it explicitly first (Console or admin API) with the values you want, or
- Change it after the fact in the Console → topic → **Configuration** tab

For anything production-ish, **create topics explicitly**. Auto-creation is a convenience for prototyping.

## The trade-off

**Pro of auto-create (default):** streaming pipelines and notebooks "just work" without you babysitting topic creation. Redpanda Connect, Spark streams, Flink SQL all flow naturally.

**Con of auto-create:** typos in topic names don't fail loudly anymore — they silently create a new topic. Set `producer.produce('sensros', ...)` instead of `'sensors'` and you get a `sensros` topic nobody reads. When your pipeline is stable, flipping it off adds a safety net.

## Scope

This is persistent across Redpanda restarts. It survives `docker restart redpanda`. It does **not** survive `destroy-all` (a full teardown drops all cluster state). After a fresh `spin-up`, the setting reverts to whatever `stacks/redpanda/config/redpanda.yaml` says — currently `true`.

## Next steps

- [Stream Bluesky firehose into Redpanda](/docs/tutorials/redpanda-connect/bluesky-stream/) — a streaming pipeline that relies on auto-create
- [Create a topic in Redpanda Console](/docs/tutorials/redpanda/create-topic/) — the explicit-creation path
