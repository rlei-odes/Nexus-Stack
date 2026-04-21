---
title: "Write a Kafka stream to a Bronze Delta table"
description: "Persist a Redpanda stream to a Delta table in Unity Catalog, with a checkpoint so restarts resume where they left off"
order: 4
---

# Write a Kafka stream to a Bronze Delta table

The payoff tutorial. You've read from Redpanda, parsed JSON into typed columns — now you persist the stream into a **Bronze Delta table** on Databricks so downstream jobs can read it as a normal SQL table. This is the real "landing zone" of a medallion architecture.

The new concepts compared to the previous tutorials: **Delta as a streaming sink**, **checkpoints** on a Unity Catalog volume, and **what happens on restart**.

## Prerequisites

- Spark reading Redpanda with parsed columns — see [Parse JSON from a Kafka topic](/docs/tutorials/spark/parse-json-schema/)
- Databricks workspace with **Unity Catalog** enabled. Free Edition works.
- A catalog and schema you can write to. Default Free Edition setup: `workspace.default`. Examples below use `workspace.default`.
- A **UC volume** for checkpoint storage. If you don't have one, create it:

```sql
%sql
CREATE VOLUME IF NOT EXISTS workspace.default.checkpoints;
```

## Define the Bronze table

Bronze is the raw-landing layer. The rule: **store the source data as close to verbatim as possible**. Parse just enough to make it queryable; don't filter, don't aggregate, don't drop fields. That's Silver's job.

```sql
%sql
CREATE TABLE IF NOT EXISTS workspace.default.bronze_sensors (
    kafka_ts TIMESTAMP,
    partition INT,
    offset BIGINT,
    sensor STRING,
    reading DOUBLE,
    event_ts BIGINT,
    ingest_ts TIMESTAMP DEFAULT current_timestamp()
) USING DELTA;
```

Two things worth noting:

- **`ingest_ts` with `DEFAULT current_timestamp()`** — when the row was written to Delta. Distinct from `event_ts` (when the event happened) and `kafka_ts` (when Kafka received it). Useful for troubleshooting pipeline delays.
- **`USING DELTA`** — explicit. Delta is the default on Databricks but writing it once means the intent is clear to anyone reading the DDL.

## The streaming write

Starting from the parsed stream from the previous tutorial:

```python
from pyspark.sql.functions import col, from_json

# ... kafka_options, sensor_schema as before ...

parsed = (
    spark.readStream
      .format("kafka")
      .options(**kafka_options)
      .load()
      .select(
          col("timestamp").alias("kafka_ts"),
          col("partition"),
          col("offset"),
          from_json(col("value").cast("string"), sensor_schema).alias("data"),
      )
      .select(
          "kafka_ts", "partition", "offset",
          "data.sensor",
          "data.reading",
          col("data.timestamp").alias("event_ts"),
      )
)

query = (
    parsed.writeStream
      .format("delta")
      .outputMode("append")
      .option("checkpointLocation", "/Volumes/workspace/default/checkpoints/bronze_sensors")
      .trigger(processingTime="10 seconds")
      .toTable("workspace.default.bronze_sensors")
)
```

Three options that matter:

### `checkpointLocation`

A directory Spark uses to persist **query state** between micro-batches:
- Which Kafka offsets have been read (so restart resumes correctly)
- Which micro-batches have been committed to Delta (so writes are exactly-once)
- Schema information

**Per-query, unique.** Give every streaming query its own checkpoint directory. Never share. If you do, you get undefined behavior — Spark complains loudly if it can but not always before damage is done.

**On a UC Volume** — `/Volumes/<catalog>/<schema>/<volume>/...`. This is the Databricks-specific path that Unity Catalog governs. Persistent across cluster restarts, which is the whole point.

### `trigger(processingTime="10 seconds")`

Start a new micro-batch every 10 seconds. Without this, Spark runs a new micro-batch as soon as the previous one finishes — effectively continuous, but with very small batches and lots of overhead on quiet streams.

10 seconds is a reasonable default for "near-real-time" ingestion. Higher values (1 minute, 5 minutes) make each batch larger and cheaper; lower values approach continuous ingestion but increase cost.

### `toTable(...)`

The sink — a Unity-Catalog-registered Delta table. The streaming write appends new rows to this table; anyone can read it as a normal SQL table (`SELECT * FROM workspace.default.bronze_sensors`) simultaneously.

## Verify

In a separate cell or notebook, while the query is running:

```sql
%sql
SELECT COUNT(*), MIN(kafka_ts), MAX(kafka_ts)
FROM workspace.default.bronze_sensors;
```

Run every ~15 seconds. Count grows, `MAX(kafka_ts)` stays within a few seconds of wall-clock — the sign of a healthy streaming ingestion.

```sql
%sql
SELECT sensor, COUNT(*) AS n, AVG(reading) AS avg_reading
FROM workspace.default.bronze_sensors
WHERE kafka_ts > current_timestamp() - INTERVAL 5 MINUTES
GROUP BY sensor;
```

Same query you might have run over an in-memory sink earlier — but now it works against persisted storage and survives cluster restarts.

## Stop, restart, see what happens

```python
query.stop()
```

Wait a minute. Producer keeps running (messages pile up in the Kafka topic). Then restart the same query — same `checkpointLocation`, same `toTable`:

```python
query = (
    parsed.writeStream
      .format("delta")
      .outputMode("append")
      .option("checkpointLocation", "/Volumes/workspace/default/checkpoints/bronze_sensors")
      .trigger(processingTime="10 seconds")
      .toTable("workspace.default.bronze_sensors")
)
```

Spark reads the checkpoint, sees where it left off, and **resumes from the exact offsets it was at**. Messages produced during the downtime are ingested in the next batch. No duplicates, no gaps — that's the exactly-once semantics you paid the checkpoint cost for.

**Verify with `DESCRIBE HISTORY`:**

```sql
%sql
DESCRIBE HISTORY workspace.default.bronze_sensors;
```

Every row = one committed streaming micro-batch. Columns show operation type (`STREAMING UPDATE`), operation metrics (rows added, bytes written), the version number, and the timestamp. Useful for both debugging and audit.

## When something goes wrong

**Query fails with `OffsetOutOfRangeException`** — the Kafka topic's retention expired while the query was down, and the offsets in the checkpoint no longer exist. Either increase retention on the topic, or accept the gap: drop the checkpoint directory and restart (you'll lose offset continuity but the query will resume reading live).

**Query fails with `CheckpointLocation already in use`** — another streaming query is using the same checkpoint. This is a safety check. Stop the other query or use a different checkpoint.

**Query runs but Delta table is empty** — check for silent JSON parse failures (all columns from the payload are NULL). See [Parse JSON with a schema](/docs/tutorials/spark/parse-json-schema/) for the `bad_rows` pattern.

**Streaming metrics look stuck** — click the "Metrics" tab of the streaming query in the notebook's Query Progress view. If `numInputRows = 0` batch after batch, the source isn't producing. If it's high but output is slow, Delta writes might be the bottleneck (check disk I/O on the cluster).

## Schema evolution

Upstream adds a new field. What happens?

With the current DDL + `append` write, **nothing breaks** — `from_json` returns NULL for fields not in your schema, which just silently drops them. To capture them, you need to:

1. Update the `sensor_schema` to include the new field (`nullable=True`)
2. Run `ALTER TABLE workspace.default.bronze_sensors ADD COLUMN new_field STRING;`
3. Spark picks up new values on the next batch

If you want Delta to auto-evolve: add `.option("mergeSchema", "true")` to the write. Then any new columns in the DataFrame are added to the table automatically. Useful in Bronze, **dangerous** downstream — don't enable it in Silver or Gold.

## What you've built

- A streaming pipeline from Redpanda → Spark → Delta, with exactly-once guarantees
- A queryable SQL table that other notebooks, jobs, or BI tools can consume in parallel
- Restart-safe state in a Unity Catalog volume

This is "Bronze" in the medallion architecture: raw landing, preserves fidelity, queryable. Silver is the layer you build next — deduplicated, typed, joined with reference data. Gold is business-level aggregations. Each layer reads from the previous via the same streaming primitives you've seen.

## Next steps

- **Silver layer** — read from `bronze_sensors` with `readStream.table(...)`, dedupe, enrich, write to `silver_sensors` with a separate checkpoint
- **Multiple topics** — extend `kafka_options.subscribe` to a comma-separated list, or use `subscribePattern` with a regex
- **Production hardening** — structured error handling, alerting on lag, monitoring via streaming metrics API
