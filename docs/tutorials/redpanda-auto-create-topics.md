---
title: "Enable auto-create topics in Redpanda"
description: "One curl call to let producers and pipelines create topics on the fly, without going through the Console"
order: 5
---

# Enable auto-create topics in Redpanda

By default, Nexus-Stack's Redpanda rejects writes to topics that don't exist — you have to create them explicitly in the [Console](/docs/tutorials/redpanda-console-basics/) first. That's the safe default, but it's annoying when:

- You're prototyping and creating lots of throwaway topics
- A Redpanda Connect pipeline writes to a topic that doesn't exist yet
- Tooling (Flink, Spark, notebooks) expects topics to appear automatically

This tutorial is one `curl` call that flips the switch cluster-wide.

## Prerequisites

- Nexus-Stack with `redpanda` and `code-server` enabled
- Familiar with the code-server terminal — see [Run curl in the code-server terminal](/docs/tutorials/code-server-terminal-curl/)

## Enable auto-creation

In a code-server terminal:

```bash
curl -s -X PUT http://redpanda:9644/v1/cluster_config \
  -H "Content-Type: application/json" \
  -d '{"upsert": {"auto_create_topics_enabled": true}, "remove": []}'
```

Expected response:

```json
{"config_version":N}
```

(Where `N` is some number — the config revision increments every time you change cluster config.)

That's it. From now on, the first `producer.produce()` or pipeline write to a non-existent topic creates it automatically with default settings (1 partition, replication factor 1, 1-week retention).

## Verify it worked

```bash
curl -s http://redpanda:9644/v1/cluster_config | python3 -m json.tool | grep auto_create_topics
```

Expected:
```
    "auto_create_topics_enabled": true,
```

## Disable it again

Symmetric call, just flip the value:

```bash
curl -s -X PUT http://redpanda:9644/v1/cluster_config \
  -H "Content-Type: application/json" \
  -d '{"upsert": {"auto_create_topics_enabled": false}, "remove": []}'
```

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

**Pro:** streaming pipelines and notebooks "just work" without you babysitting topic creation.

**Con:** typos in topic names don't fail loudly anymore — they silently create a new topic. Set `producer.produce('sensros', ...)` instead of `'sensors'` and you get a `sensros` topic nobody reads. Flip back to `false` once your pipeline is stable.

## Scope

This is persistent across Redpanda restarts. It survives `docker restart redpanda`. It does **not** survive `destroy-all` (a full teardown drops all cluster state). After a fresh `spin-up`, you'll need to run this again if you want it on.

## Next steps

- [Stream Bluesky firehose into Redpanda](/docs/tutorials/bluesky-to-redpanda-connect/) — the most common use case that needs this
- [Create a topic in Redpanda Console](/docs/tutorials/redpanda-create-topic/) — the explicit alternative
