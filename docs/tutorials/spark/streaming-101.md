---
title: "Spark Structured Streaming 101"
description: "First streaming query in Databricks with the rate source and memory sink — no Kafka required"
order: 1
---

# Spark Structured Streaming 101

Before connecting Spark to a real streaming source (Redpanda, Kafka), it's worth seeing the core streaming primitives in isolation. This tutorial uses the built-in **`rate` source** — a generator that emits `N` synthetic rows per second — and the **`memory` sink** so results land in a queryable in-memory table. No external services involved; everything runs inside Databricks.

About 15 minutes. Run it once, you've seen the whole mental model of Structured Streaming.

## Prerequisites

- Databricks workspace (Free Edition works — no Unity Catalog required for this tutorial)
- A running compute / cluster (Free Edition's serverless default is fine)
- A Python notebook attached to the cluster

## The three pieces of any streaming query

Every Structured Streaming query has exactly three pieces:

1. **A streaming source** — where data comes from (`rate`, `kafka`, a directory of files, a Delta table)
2. **Transformations** — `.select()`, `.filter()`, `.groupBy(...)`, `.withWatermark(...)`, etc. Same DataFrame API as batch.
3. **A streaming sink** — where results go (`memory`, `console`, `kafka`, Delta)

The query runs continuously, processing new data as it arrives, until you stop it.

## Hello, rate source

In a new notebook cell:

```python
# Read from the built-in rate source: 5 rows/second, each with timestamp + value
events = (
    spark.readStream
      .format("rate")
      .option("rowsPerSecond", 5)
      .load()
)

events.printSchema()
```

Output:
```
root
 |-- timestamp: timestamp (nullable = true)
 |-- value: long (nullable = true)
```

`events` is a **streaming DataFrame**. It looks identical to a batch DataFrame, except you can't call `.collect()` or `.show()` on it — there's no "all the rows", it's an unbounded stream.

## Write to memory so you can SELECT

```python
query = (
    events.writeStream
      .format("memory")
      .queryName("my_events")     # the name used in SQL
      .outputMode("append")       # new rows are appended, not updated
      .start()
)
```

`query.start()` returns immediately — the streaming query runs in the background on the cluster. A new in-memory table `my_events` starts filling with `timestamp, value` rows at 5/sec.

## Query the live stream

In a new cell, `%sql`:

```sql
%sql
SELECT COUNT(*) FROM my_events;
```

Run it a few times — the count increases by ~5 every second. You're reading a live streaming query's output with a standard batch SQL query.

Try a more interesting one:

```sql
%sql
SELECT
  date_trunc('second', timestamp) AS sec,
  COUNT(*) AS n
FROM my_events
GROUP BY sec
ORDER BY sec DESC
LIMIT 5;
```

Most recent seconds, newest first, each with `n ≈ 5`.

## Stop the query

```python
query.stop()
```

Writing to `memory` holds rows in driver memory — if you don't stop the query, it eats driver RAM until the cluster is restarted. Always stop memory-sink queries when done.

## A slightly real transformation

Restart with a filter + groupBy:

```python
events = (
    spark.readStream
      .format("rate")
      .option("rowsPerSecond", 5)
      .load()
)

# Only odd-valued rows; aggregate per 10-second window
from pyspark.sql.functions import window, col

windowed = (
    events.filter(col("value") % 2 == 1)
          .groupBy(window("timestamp", "10 seconds"))
          .count()
)

query = (
    windowed.writeStream
      .format("memory")
      .queryName("windowed_events")
      .outputMode("update")             # emit new or changed rows per window
      .start()
)
```

- **`outputMode("update")`** — when windows update, emit the new count for that window. `append` would wait until a window is definitely final before emitting. `update` is the right mode for most live-dashboard cases.
- **`window("timestamp", "10 seconds")`** — bucket rows into 10-second tumbling windows using the `timestamp` column.

Check the output:

```sql
%sql
SELECT window, count
FROM windowed_events
ORDER BY window DESC
LIMIT 5;
```

Each row has a `window.start` / `window.end` struct and a count ≈ 25 (half of 50 rows across 10 seconds, since we filtered to odd-only).

Stop when done:

```python
query.stop()
```

## Checkpoint (the thing that makes it fault-tolerant)

The memory sink doesn't need one. Every other sink does. A **checkpoint location** is a directory where Spark persists streaming state: offsets of what's been read, per-key aggregation state, committed batches. When a streaming query restarts, it reads the checkpoint and resumes exactly where it left off.

Where to put it depends on your workspace:

```python
# Unity Catalog workspace (Free Edition, Premium with UC)
.option("checkpointLocation", "/Volumes/workspace/default/checkpoints/my_query")

# Non-UC workspace or throwaway demos
.option("checkpointLocation", "dbfs:/tmp/spark-checkpoints/my_query")
```

- **UC Volumes** (`/Volumes/...`) — the right answer for persistent production pipelines; the volume is governed, tracked, and survives cluster restarts. The [Bronze Delta tutorial](/docs/tutorials/spark/bronze-delta/) uses this path.
- **DBFS** (`dbfs:/...`) — the pre-UC Databricks filesystem. Fine for development and throwaway queries. Write access is governed by workspace ACLs rather than UC.

Checkpoint location is per-query. Give each query its own directory. Never share a checkpoint across queries — results are undefined.

## The triggers

A streaming query has a **trigger** that controls how often a new micro-batch starts:

- **Default** — start a new micro-batch as soon as the previous one finishes. Effectively continuous.
- **`.trigger(processingTime="5 seconds")`** — start a new micro-batch every 5 seconds (wait if previous isn't done).
- **`.trigger(once=True)`** — older single-batch option, still supported. Runs exactly one micro-batch then stops. Superseded by `availableNow`.
- **`.trigger(availableNow=True)`** — recommended modern choice for bounded catch-up runs: process everything currently available, then stop. Unlike `once`, it can span multiple internal batches if the backlog is large.

For real-time dashboards, default or `processingTime="2 seconds"`. For batchy pipelines that run on a cron, `availableNow`.

## Output modes explained

| Mode | When to use | What it does |
|---|---|---|
| **`append`** | Only new rows, never updates | Emits a row once, never changes it. Works with most transformations including filter and select. |
| **`update`** | Aggregations where current values matter | Emits rows that are new **or** changed since last batch. Needs a stateful sink. |
| **`complete`** | Small dashboards only | Emits the **entire** result table every batch. Only works with aggregations. Don't use for big data. |

Structured Streaming enforces compatibility: `append` with a `groupBy` fails — no way to know when an aggregate is "final" without a watermark. Add `.withWatermark()` to relax that (see the Parse JSON tutorial).

## What you just learned

- A streaming query = `readStream + transforms + writeStream.start()`
- Memory sink for exploration, real sinks (Kafka, Delta) for production
- Output modes match the transformation shape
- Checkpoint is how streaming stays correct across restarts

Now you can read anything Spark knows about — including Redpanda.

## Next steps

- [Read a Redpanda topic from Spark](/docs/tutorials/spark/read-redpanda/) — same pattern, real source
- [Parse JSON with a schema](/docs/tutorials/spark/parse-json-schema/) — turn Kafka's raw bytes into typed columns
