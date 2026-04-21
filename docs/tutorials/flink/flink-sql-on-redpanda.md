---
title: "Query a Redpanda topic with Flink SQL in Dinky"
description: "CREATE TABLE with the Kafka connector, first streaming SELECT, and a windowed aggregation"
order: 2
---

# Query a Redpanda topic with Flink SQL in Dinky

Now that [Dinky is set up](/docs/tutorials/flink/dinky-setup/) and talking to Flink, the natural next step is reading from Redpanda. This tutorial walks through creating a Flink SQL **source table** pointed at a Kafka topic, running a continuous `SELECT`, and a windowed aggregation — all in Dinky's Data Studio.

## Prerequisites

- Dinky registered against Flink — see [Dinky first-time setup](/docs/tutorials/flink/dinky-setup/)
- A topic with messages — either produce your own via the [Python producer tutorial](/docs/tutorials/redpanda/first-producer/), or stream [Bluesky into Redpanda](/docs/tutorials/redpanda-connect/bluesky-stream/). Examples below assume a topic called `sensors` with JSON messages of shape `{"sensor": "...", "reading": 42.5, "timestamp": 1713...}`

## Create the task

In Dinky → **Data Studio** → **+** → new task:

- **Task Type:** `FlinkSQL`
- **Name:** `redpanda-sensors` (or anything)

In the task's config panel (right side):
- **Catalog:** `DefaultCatalog`
- **Cluster:** your registered Flink cluster (e.g. `nexus-flink (standalone)`)

## Define the source table

Flink doesn't "have" a topic — you describe it with a `CREATE TABLE` statement that tells Flink how to read from Kafka, what columns to expect, and what format the payload is in.

Paste into the editor:

```sql
CREATE TABLE IF NOT EXISTS sensors (
    sensor STRING,
    reading DOUBLE,
    `timestamp` BIGINT,
    proc_time AS PROCTIME()          -- computed column: processing time
) WITH (
    'connector' = 'kafka',
    'topic' = 'sensors',
    'properties.bootstrap.servers' = 'redpanda:9092',
    'properties.group.id' = 'dinky-sensors-reader',
    'scan.startup.mode' = 'latest-offset',
    'format' = 'json'
);
```

Execute the cell — it should return instantly with "success" in the console. The table is now registered in Flink's catalog. This statement only needs to run **once** per catalog; the `DefaultCatalog` persists table definitions across tasks.

### What each `WITH` option does

- **`connector = 'kafka'`** — Nexus-Stack's Flink image ships with the Kafka SQL connector JAR baked in (added via `stacks/flink/Dockerfile` — vanilla `flink:1.x` doesn't include it and would need the JAR added separately). Works against Redpanda since it's Kafka wire-compatible.
- **`topic`** — which Kafka topic to read from
- **`properties.bootstrap.servers`** — same hostname as your Python clients (`redpanda:9092` inside the Docker network)
- **`properties.group.id`** — Kafka consumer group. If you have multiple Flink jobs reading the same topic, give each a different group ID so they each see all messages independently.
- **`scan.startup.mode`** — where to start reading. `latest-offset` = only new messages from now. `earliest-offset` = replay the whole topic. `timestamp` / `specific-offsets` for more control.
- **`format = 'json'`** — deserialize message values as JSON and map fields to column names

### What `PROCTIME()` does

`proc_time AS PROCTIME()` is a **computed column** that gives every row a timestamp of when Flink processed it. Required for processing-time windows below. Event-time windows use a watermark on a real timestamp column instead — more robust, more setup.

## Run a continuous SELECT

New task (or same task, new query block). Execute:

```sql
SELECT sensor, reading, `timestamp` FROM sensors;
```

Click **Execute**. Unlike a regular database query, this **doesn't return and finish** — Flink runs it as a streaming job. Results start appearing in the **Result** tab as new messages arrive on the topic.

To stop: click the **red stop** button on the query panel (or go to **Running Jobs** in Dinky and stop from there).

## Windowed aggregation

The equivalent of the Python consumer aggregator, in one statement:

```sql
SELECT
    sensor,
    COUNT(*) AS event_count,
    AVG(reading) AS avg_reading,
    TUMBLE_START(proc_time, INTERVAL '10' SECOND) AS window_start
FROM sensors
GROUP BY sensor, TUMBLE(proc_time, INTERVAL '10' SECOND);
```

Execute. The **Result** tab updates every 10 seconds with one row per `(sensor, 10-second-window)`. Flink handles:

- **Windowing** — bucketing events by processing time
- **State** — accumulating per-window counts and sums
- **Emission** — outputting a row when each window closes
- **Restart safety** — if the job crashes, Flink resumes from checkpoint (needs additional config for production)

Three lines that would be ~50 lines of Python, plus scaling, plus reliability.

## Filter and write back to Redpanda

You can also use Flink as a **router** — read from one topic, filter/transform, write to another. Example: put all English-language posts from the Bluesky stream into their own topic.

First, declare the sink table (one-time):

```sql
CREATE TABLE IF NOT EXISTS sensors_high (
    sensor STRING,
    reading DOUBLE,
    `timestamp` BIGINT
) WITH (
    'connector' = 'kafka',
    'topic' = 'sensors-high',
    'properties.bootstrap.servers' = 'redpanda:9092',
    'format' = 'json'
);
```

Then the `INSERT INTO` statement (which becomes a long-running job):

```sql
INSERT INTO sensors_high
SELECT sensor, reading, `timestamp`
FROM sensors
WHERE reading > 28;
```

Execute. This is a **streaming INSERT** — it runs indefinitely, consuming from `sensors` and writing to `sensors-high` as new high-reading events arrive.

Verify in the Redpanda Console → **Topics** → `sensors-high` → **Messages** tab. Rows appear live.

**Stop the job** when done via Dinky's Running Jobs page or the red stop button — otherwise it keeps one of your 2 Flink task slots occupied indefinitely.

## Task slot limit (the thing that trips everyone up)

Nexus-Stack's Flink is configured with **2 task slots**. Each running streaming job (INSERT INTO) uses 1 slot. Each interactive `SELECT` also uses 1 slot while you have its Result tab open.

If you try to start a 3rd job, it queues or fails. Symptoms:
- "Not enough free slots available" error
- Query appears to execute but no results arrive

**Fix:** go to Dinky → **Operations Center** → **Running Jobs** → stop jobs you no longer need (red stop button on each row).

## When to use Flink SQL vs a Python consumer

| Use case | Best tool |
|---|---|
| "I want to see what's on this topic" | Python consumer or Redpanda Console |
| "Quick one-off transformation" | Redpanda Connect |
| "Windowed aggregation, small scale" | Python consumer (didactic — but hand-rolled state) |
| "Windowed aggregation, production" | **Flink SQL** — state, exactly-once, scale-out |
| "Complex multi-stream join" | **Flink SQL** — the real use case for Flink |
| "Feed a data warehouse / lakehouse" | **Flink SQL** or Spark Structured Streaming |

## Next steps

- [Spark Structured Streaming 101](/docs/tutorials/spark/streaming-101/) — the Databricks-side equivalent of this
- [Manage Redpanda Connect streams](/docs/tutorials/redpanda-connect/manage-streams/) — for pipelines without SQL
